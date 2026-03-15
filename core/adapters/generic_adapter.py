"""GenericAdapter — production implementation for unknown / generic job sites.

Design rules
------------
- DOM first, Vision fallback only for field identification (what field is this?)
- Vision NEVER drives navigation decisions or clicks directly
- Browser lifecycle is owned here: opened in plan(), closed in cleanup()
- Shared page/context survives across all step calls on the same adapter instance
- Auth (login/signup) is delegated to credential_manager + applicator helpers
- Field filling reuses battle-tested helpers from core/applicator.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from openai import OpenAI

from core.adapters.base_adapter import (
    AdapterResult, BaseAdapter, PageState,
    dom_detect_page_state, _dom_detect_captcha, _visible,
)
from core.orchestrator import AdapterBase, ApplyState, StepResult, register_adapter

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page


# ── Constants ─────────────────────────────────────────────────────────────────

_MAX_FORM_PAGES = 12        # safety cap on multi-step form loop
_WAIT_AFTER_CLICK = 1500   # ms to wait after button clicks
_PAGE_LOAD_TIMEOUT = 60_000


# ── GenericAdapter ────────────────────────────────────────────────────────────

class GenericAdapter(AdapterBase, BaseAdapter):
    """Handles any job site without a dedicated adapter.

    Inherits from:
      AdapterBase (orchestrator.py) — state machine interface
      BaseAdapter (base_adapter.py) — browser-level interface

    The orchestrator calls plan() → login()/signup() → fill_form() → submit().
    Each call reuses self._page (one browser, one context, one page per run).
    """

    name = "generic"

    @classmethod
    def detect(cls, url: str) -> bool:
        return True   # final fallback — accepts everything

    # ── Browser state (shared across all step calls) ──────────────────────────

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._client: OpenAI | None = None
        self._screenshots: list[str] = []
        self._cover_letter: str | None = None
        self._platform_key: str | None = None

    # ── Orchestrator interface (AdapterBase) ──────────────────────────────────
    # Each method maps an orchestrator state to a browser-level action.

    def plan(self, checkpoint_meta: dict) -> StepResult:
        """Open browser, navigate to apply_url, detect page state."""
        try:
            self._open_browser()
            page_state = self._navigate_and_detect(self.apply_url, label="01_plan")
            return self._page_state_to_step_result(page_state, checkpoint_meta)
        except Exception as exc:
            logger.exception(f"[{self.job_hash[:8]}] GenericAdapter.plan failed: {exc}")
            return StepResult(ApplyState.FAILED, success=False, error=str(exc))

    def restore_session(self, checkpoint_meta: dict) -> StepResult:
        """Try saved cookies; fall through to LOGIN if session is stale."""
        from core.credential_manager import load_session_state
        from urllib.parse import urlparse

        domain = urlparse(self.apply_url).hostname or ""
        session_json = load_session_state(domain)
        if not session_json:
            return StepResult(ApplyState.LOGIN)

        try:
            ok = self.restore_session(self._page, session_json, self._build_context())
            if ok:
                logger.info(f"[{self.job_hash[:8]}] Session restored for {domain}")
                page_state = dom_detect_page_state(self._page)
                if page_state.kind == "form":
                    return StepResult(ApplyState.FILL_FORM)
            return StepResult(ApplyState.LOGIN)
        except Exception as exc:
            logger.warning(f"Session restore failed: {exc}")
            return StepResult(ApplyState.LOGIN)

    def login(self, checkpoint_meta: dict) -> StepResult:
        """Fill login form with stored credentials."""
        from core.credential_manager import (
            get_credential, resolve_platform_key, mark_login_success,
            mark_account_status,
        )

        # Screenshot before we touch the login form (useful for debugging failures)
        before_shot = self._safe_screenshot("before_login")
        if before_shot:
            self._screenshots.append(before_shot)

        platform_key = self._platform_key or resolve_platform_key(self.apply_url)
        cred = get_credential(platform_key)
        if not cred:
            logger.info(f"[{self.job_hash[:8]}] No stored credentials for {platform_key} — signup")
            return StepResult(ApplyState.SIGNUP)

        email, password = cred
        result = self._do_login(email, password)

        if result.screenshot_path:
            self._screenshots.append(result.screenshot_path)

        if result.requires_verification:
            mark_account_status(platform_key, "pending_verification")
            return StepResult(
                ApplyState.VERIFY,
                meta={"platform_key": platform_key, **result.metadata},
            )
        if result.success:
            mark_login_success(platform_key)
            return StepResult(
                ApplyState.FILL_FORM,
                screenshot_path=result.screenshot_path,
                meta=result.metadata,
            )
        # Login failed — try signup
        logger.warning(f"[{self.job_hash[:8]}] Login failed: {result.error_message}")
        return StepResult(
            ApplyState.SIGNUP,
            success=False,
            error=result.error_message,
            screenshot_path=result.screenshot_path,
        )

    def signup(self, checkpoint_meta: dict) -> StepResult:
        """Create a new account."""
        from core.credential_manager import (
            resolve_platform_key, save_credential, generate_secure_password,
            PLATFORMS_NO_AUTO_SIGNUP,
        )
        from core.applicator import _get_answers

        platform_key = self._platform_key or resolve_platform_key(self.apply_url)
        if platform_key in PLATFORMS_NO_AUTO_SIGNUP:
            return StepResult(
                ApplyState.HUMAN_INTERVENTION,
                success=False,
                error=f"Auto-signup disabled for {platform_key} — manual login required",
            )

        answers = _get_answers()
        email = answers.get("email", os.environ.get("GMAIL_ADDRESS", ""))
        password = generate_secure_password(20)

        result = self._do_signup(email, password)
        if result.screenshot_path:
            self._screenshots.append(result.screenshot_path)

        if result.success:
            save_credential(platform_key, email, password,
                            domain=self._page.url if self._page else "",
                            auth_type="password")
            if result.requires_verification:
                return StepResult(
                    ApplyState.VERIFY,
                    meta={"platform_key": platform_key, **result.metadata},
                )
            return StepResult(ApplyState.FILL_FORM,
                              screenshot_path=result.screenshot_path,
                              meta=result.metadata)

        return StepResult(
            ApplyState.FAILED,
            success=False,
            error=result.error_message,
            screenshot_path=result.screenshot_path,
        )

    def verify(self, checkpoint_meta: dict) -> StepResult:
        """Handle OTP / CAPTCHA / email-link verification.

        Strategy (in order):
        1. CAPTCHA present → HUMAN_INTERVENTION immediately
        2. Auto-verify via IMAP (silent, no user action needed)
        3. OTP input visible on page → ask user via WhatsApp, poll for reply
        4. No OTP input (email-link only) → ask user to click link, send DONE
        """
        from core.verifier import (
            OTP_SELECTORS, CAPTCHA_SELECTORS, OTP_SUBMIT_TEXTS,
            request_otp_from_user, poll_for_otp,
            request_human_intervention, clear_verification_state,
            VERIFY_TIMEOUT_SECONDS,
        )
        from core.credential_manager import resolve_platform_key

        platform_key = (
            checkpoint_meta.get("platform_key")
            or self._platform_key
            or resolve_platform_key(self.apply_url)
        )

        shot = self._safe_screenshot("verify_page")
        if shot:
            self._screenshots.append(shot)

        page = self._page
        if not page:
            return StepResult(ApplyState.FAILED, success=False, error="No browser page in verify()")

        # ── 1. CAPTCHA ─────────────────────────────────────────────────────────
        for sel in CAPTCHA_SELECTORS:
            try:
                if page.locator(sel).count() > 0:
                    shot2 = self._safe_screenshot("captcha_page")
                    if shot2:
                        self._screenshots.append(shot2)
                    request_human_intervention(
                        self.job_hash, self.company, self.apply_url,
                        "CAPTCHA detected — please solve it and send DONE",
                    )
                    return StepResult(
                        ApplyState.HUMAN_INTERVENTION, success=False,
                        error="CAPTCHA requires manual action",
                        screenshot_path=shot2 or shot,
                    )
            except Exception:
                continue

        # ── 2. Auto-verify via IMAP ────────────────────────────────────────────
        try:
            from core.applicator import _auto_verify_email, _screenshot as _ashot
            result_inner: dict = {}
            ok, _ = _auto_verify_email(
                platform_key, page, self._client,
                self.job_hash[:8], 1, result_inner,
            )
            if ok:
                auto_shot = str(_ashot(page, self.job_hash[:8], "verify_auto_done"))
                self._screenshots.append(auto_shot)
                logger.info(f"[{self.job_hash[:8]}] Email verified automatically via IMAP")
                return StepResult(ApplyState.FILL_FORM, screenshot_path=auto_shot)
        except Exception as exc:
            logger.debug(f"[{self.job_hash[:8]}] IMAP auto-verify not available: {exc}")

        # ── 3. OTP input on page → ask user via WhatsApp ───────────────────────
        otp_loc = None
        for sel in OTP_SELECTORS:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible(timeout=500):
                    otp_loc = loc.first
                    break
            except Exception:
                continue

        if otp_loc is not None:
            platform_name = platform_key.removeprefix("generic:") or "the site"
            request_otp_from_user(self.job_hash, self.company, platform_name, self.apply_url)
            otp_code = poll_for_otp(self.job_hash, VERIFY_TIMEOUT_SECONDS)

            if not otp_code:
                clear_verification_state()
                shot3 = self._safe_screenshot("otp_timeout")
                return StepResult(
                    ApplyState.FAILED, success=False,
                    error="OTP timeout — no user response within 5 minutes",
                    screenshot_path=shot3 or shot,
                )

            try:
                otp_loc.fill(otp_code)
                for text in OTP_SUBMIT_TEXTS:
                    btn = page.locator(f'button:has-text("{text}")')
                    try:
                        if btn.count() > 0 and btn.first.is_visible(timeout=500):
                            btn.first.click()
                            break
                    except Exception:
                        continue
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception as exc:
                logger.warning(f"[{self.job_hash[:8]}] OTP fill error: {exc}")
                return StepResult(ApplyState.FAILED, success=False,
                                  error=f"OTP fill failed: {exc}")
            finally:
                # Explicit delete so the OTP string isn't reachable from the frame
                del otp_code

            shot4 = self._safe_screenshot("after_otp")
            if shot4:
                self._screenshots.append(shot4)
            return StepResult(ApplyState.FILL_FORM, screenshot_path=shot4 or shot)

        # ── 4. Email-link verification (no OTP input on page) ──────────────────
        request_human_intervention(
            self.job_hash, self.company, self.apply_url,
            "Email verification link required — please click the link in your email, then send DONE",
        )
        return StepResult(
            ApplyState.HUMAN_INTERVENTION, success=False,
            error="Email verification link required — waiting for user",
            screenshot_path=shot,
        )

    def fill_form(self, checkpoint_meta: dict) -> StepResult:
        """Multi-step form filling — deterministic selectors + Vision for field mapping."""
        try:
            result = self._do_fill_form()
            if result.screenshot_path:
                self._screenshots.append(result.screenshot_path)
            next_st = ApplyState(result.next_state)
            return StepResult(
                next_st,
                success=result.success,
                error=result.error_message,
                screenshot_path=result.screenshot_path,
                meta=result.metadata,
            )
        except Exception as exc:
            logger.exception(f"[{self.job_hash[:8]}] fill_form() failed: {exc}")
            shot = self._safe_screenshot("fill_form_crash")
            return StepResult(ApplyState.FAILED, success=False,
                              error=str(exc), screenshot_path=shot)

    def review(self, checkpoint_meta: dict) -> StepResult:
        """Accept review page (no-op for generic sites)."""
        return StepResult(ApplyState.SUBMIT)

    def submit(self, checkpoint_meta: dict) -> StepResult:
        """Click the final submit button and confirm success."""
        try:
            result = self._do_submit()
            if result.screenshot_path:
                self._screenshots.append(result.screenshot_path)
            next_st = ApplyState(result.next_state)
            return StepResult(
                next_st,
                success=result.success,
                error=result.error_message,
                screenshot_path=result.screenshot_path,
                meta={"screenshots": self._screenshots, **result.metadata},
            )
        except Exception as exc:
            logger.exception(f"[{self.job_hash[:8]}] submit() failed: {exc}")
            shot = self._safe_screenshot("submit_crash")
            return StepResult(ApplyState.FAILED, success=False,
                              error=str(exc), screenshot_path=shot)

    def cleanup(self, final_state: ApplyState, error: str | None) -> None:
        """Save session state and close browser."""
        if self._page and self._browser:
            try:
                from core.credential_manager import save_session_state
                from urllib.parse import urlparse
                from datetime import datetime, timezone, timedelta

                domain = urlparse(self.apply_url).hostname or ""
                storage_state = self._context.storage_state()
                save_session_state(
                    domain=domain,
                    platform_key=self._platform_key or "generic",
                    storage_state=json.dumps(storage_state),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                )
            except Exception as exc:
                logger.debug(f"Session save skipped: {exc}")
            try:
                self._browser.close()
            except Exception:
                pass
            try:
                self._playwright.stop()
            except Exception:
                pass
        logger.info(
            f"[{self.job_hash[:8]}] GenericAdapter cleanup "
            f"final={final_state.value} err={error!r}"
        )

    # ── BaseAdapter interface (browser-level, called by the methods above) ────

    def analyze_entrypoint(self, page: "Page", context: dict) -> PageState:
        """DOM-only page state detection."""
        return dom_detect_page_state(page)

    def restore_session(self, page: "Page", session_data: str,   # type: ignore[override]
                        context: dict) -> bool:
        """Restore saved cookies and verify the session is valid."""
        try:
            state = json.loads(session_data)
            self._context.add_cookies(state.get("cookies", []))
            page.reload(wait_until="domcontentloaded", timeout=_PAGE_LOAD_TIMEOUT)
            page.wait_for_timeout(2000)
            ps = dom_detect_page_state(page)
            return ps.kind not in ("login", "signup", "unknown")
        except Exception as exc:
            logger.debug(f"restore_session failed: {exc}")
            return False

    def login(self, page: "Page", email: str, password: str,      # type: ignore[override]
              context: dict) -> AdapterResult:
        raise NotImplementedError("use _do_login()")

    def signup(self, page: "Page", email: str, password: str,     # type: ignore[override]
               context: dict) -> AdapterResult:
        raise NotImplementedError("use _do_signup()")

    def fill_form(self, page: "Page", context: dict) -> AdapterResult:  # type: ignore[override]
        raise NotImplementedError("use _do_fill_form()")

    def submit(self, page: "Page", context: dict) -> AdapterResult:  # type: ignore[override]
        raise NotImplementedError("use _do_submit()")

    # ── Internal browser helpers ──────────────────────────────────────────────

    def _open_browser(self) -> None:
        """Open Playwright, browser, context, and page — idempotent."""
        if self._page:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=False, slow_mo=200
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        # Load LinkedIn cookies if relevant
        if "linkedin.com" in self.apply_url:
            session_file = (
                Path(__file__).parent.parent.parent / "data" / "linkedin_session.json"
            )
            if session_file.exists():
                cookies = json.loads(session_file.read_text())
                self._context.add_cookies(cookies)

        self._page = self._context.new_page()

        self._client = OpenAI(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        )

        from core.credential_manager import resolve_platform_key
        self._platform_key = resolve_platform_key(self.apply_url)

    def _navigate_and_detect(self, url: str, label: str) -> PageState:
        """Navigate to *url* and run DOM-based page state detection."""
        wait = "load" if "linkedin.com" in url else "networkidle"
        try:
            self._page.goto(url, wait_until=wait, timeout=_PAGE_LOAD_TIMEOUT)
        except Exception:
            self._page.goto(url, wait_until="load", timeout=_PAGE_LOAD_TIMEOUT)
        self._page.wait_for_timeout(2500)
        self._dismiss_popups()
        shot = self._safe_screenshot(label)
        if shot:
            self._screenshots.append(shot)
        state = dom_detect_page_state(self._page)
        logger.info(
            f"[{self.job_hash[:8]}] Page state after navigate: "
            f"kind={state.kind} ambiguous={state.is_ambiguous}"
        )
        if state.is_ambiguous:
            state = self._vision_classify_state(shot) if shot else state
        return state

    def _page_state_to_step_result(
        self, page_state: PageState, meta: dict
    ) -> StepResult:
        """Map a PageState to the correct next ApplyState."""
        shot = self._screenshots[-1] if self._screenshots else None
        kind = page_state.kind

        if kind == "success":
            return StepResult(ApplyState.SUCCESS, screenshot_path=shot)
        if kind == "captcha":
            return StepResult(
                ApplyState.HUMAN_INTERVENTION, success=False,
                error="CAPTCHA detected on landing page", screenshot_path=shot,
            )
        if kind == "two_fa":
            return StepResult(ApplyState.VERIFY, screenshot_path=shot)
        if kind == "login":
            return StepResult(ApplyState.RESTORE_SESSION, screenshot_path=shot)
        if kind == "signup":
            return StepResult(ApplyState.SIGNUP, screenshot_path=shot)
        if kind == "form":
            return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)
        if kind == "apply_button":
            # Click Apply button and re-detect
            clicked = self._click_apply_button(page_state.apply_button_text)
            if clicked:
                self._page.wait_for_timeout(_WAIT_AFTER_CLICK)
                shot2 = self._safe_screenshot("after_apply_click")
                if shot2:
                    self._screenshots.append(shot2)
                state2 = dom_detect_page_state(self._page)
                if state2.is_ambiguous and shot2:
                    state2 = self._vision_classify_state(shot2)
                return self._page_state_to_step_result(state2, meta)
            return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)
        # unknown — try Vision one more time then fall through to FILL_FORM
        if shot:
            state2 = self._vision_classify_state(shot)
            if not state2.is_ambiguous:
                return self._page_state_to_step_result(state2, meta)
        return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)

    # ── Login / signup DOM helpers ────────────────────────────────────────────

    def _do_login(self, email: str, password: str) -> AdapterResult:
        """DOM-first login: fill email → fill password → click submit."""
        page = self._page
        try:
            # Email
            email_sel = (
                'input[type="email"], input[autocomplete="username"], '
                'input[name*="email" i], input[id*="email" i], '
                'input[placeholder*="email" i]'
            )
            if not self._fill_input(email_sel, email, "email"):
                return AdapterResult.fail("signup", "Could not find email input on login page")

            page.wait_for_timeout(500)

            # Password
            pwd_sel = 'input[type="password"]'
            if not self._fill_input(pwd_sel, password, "password"):
                return AdapterResult.fail("failed", "Could not find password input on login page")

            page.wait_for_timeout(300)

            # Submit button
            if not self._click_login_button():
                return AdapterResult.fail("failed", "Could not find login submit button")

            page.wait_for_timeout(3000)
            shot = self._safe_screenshot("after_login")
            if shot:
                self._screenshots.append(shot)

            state = dom_detect_page_state(page)

            # Check for "not verified" text
            try:
                body = (page.locator("body").inner_text() or "").lower()
                if "not verified" in body or "verify your email" in body:
                    return AdapterResult.need_verification(
                        screenshot_path=shot,
                        metadata={"reason": "email_not_verified"},
                    )
            except Exception:
                pass

            if state.kind == "two_fa":
                return AdapterResult.need_verification(screenshot_path=shot)
            if state.kind in ("login", "signup"):
                return AdapterResult.fail(
                    "signup", "Still on login page after submit",
                    screenshot_path=shot,
                )
            if state.kind in ("form", "apply_button", "success", "unknown"):
                return AdapterResult.ok("fill_form", screenshot_path=shot)

            return AdapterResult.fail("failed", f"Unexpected page after login: {state.kind}",
                                      screenshot_path=shot)

        except Exception as exc:
            shot = self._safe_screenshot("login_error")
            return AdapterResult.fail("failed", str(exc), screenshot_path=shot)

    def _do_signup(self, email: str, password: str) -> AdapterResult:
        """DOM-first signup: find create-account link, fill form, submit."""
        from core.applicator import _get_answers, _check_consent_checkboxes
        from core.credential_manager import SIGNUP_LINK_TEXTS

        page = self._page
        answers = _get_answers()

        try:
            # Try to find and click a "Create Account" link first
            for text in SIGNUP_LINK_TEXTS:
                for role in ("link", "button"):
                    try:
                        loc = page.get_by_role(role, name=text, exact=False)
                        if loc.count() > 0 and loc.first.is_visible():
                            loc.first.click()
                            page.wait_for_timeout(2000)
                            break
                    except Exception:
                        continue

            shot = self._safe_screenshot("signup_page")
            if shot:
                self._screenshots.append(shot)

            # Fill email
            email_sel = (
                'input[type="email"], input[autocomplete="email"], '
                'input[name*="email" i], input[id*="email" i], '
                'input[placeholder*="email" i]'
            )
            self._fill_input(email_sel, email, "email")
            page.wait_for_timeout(300)

            # Password
            new_pwd_sel = (
                'input[autocomplete="new-password"], '
                'input[type="password"][name*="password" i], '
                'input[type="password"][id*="password" i], '
                'input[type="password"]'
            )
            pwd_fields = page.locator('input[type="password"]')
            if pwd_fields.count() >= 2:
                # Fill both password + confirm password
                for i in range(min(pwd_fields.count(), 2)):
                    try:
                        pwd_fields.nth(i).fill(password)
                        page.wait_for_timeout(200)
                    except Exception:
                        pass
            else:
                self._fill_input(new_pwd_sel, password, "password")

            page.wait_for_timeout(300)

            # Fill name fields if present
            if _visible(page, 'input[name*="first" i], input[id*="first" i], '
                               'input[placeholder*="first" i]'):
                self._fill_input('input[name*="first" i], input[id*="first" i], '
                                 'input[placeholder*="first" i]',
                                 answers.get("first_name", ""), "first_name")
            if _visible(page, 'input[name*="last" i], input[id*="last" i], '
                               'input[placeholder*="last" i]'):
                self._fill_input('input[name*="last" i], input[id*="last" i], '
                                 'input[placeholder*="last" i]',
                                 answers.get("last_name", ""), "last_name")

            # Consent checkboxes
            _check_consent_checkboxes(page, 1)

            page.wait_for_timeout(300)

            # Submit signup form
            signup_submit_texts = [
                "Create Account", "Sign Up", "Register", "Create account",
                "Submit", "Continue", "Next",
            ]
            submitted = False
            for text in signup_submit_texts:
                for role in ("button", "link"):
                    try:
                        loc = page.get_by_role(role, name=text, exact=False)
                        if loc.count() > 0 and loc.first.is_visible():
                            loc.first.click()
                            page.wait_for_timeout(3000)
                            submitted = True
                            break
                    except Exception:
                        continue
                if submitted:
                    break

            shot = self._safe_screenshot("after_signup")
            if shot:
                self._screenshots.append(shot)

            state = dom_detect_page_state(page)

            # Check for verification prompt
            try:
                body = (page.locator("body").inner_text() or "").lower()
                if any(kw in body for kw in (
                    "verify your email", "confirmation email", "check your email",
                    "verification code", "we sent",
                )):
                    return AdapterResult.need_verification(screenshot_path=shot)
            except Exception:
                pass

            if state.kind in ("form", "apply_button", "unknown"):
                return AdapterResult.ok("fill_form", screenshot_path=shot)
            if state.kind == "two_fa":
                return AdapterResult.need_verification(screenshot_path=shot)
            if state.kind == "success":
                return AdapterResult.ok("success", screenshot_path=shot)

            return AdapterResult.fail(
                "failed", f"Signup ended in unexpected state: {state.kind}",
                screenshot_path=shot,
            )

        except Exception as exc:
            shot = self._safe_screenshot("signup_error")
            return AdapterResult.fail("failed", str(exc), screenshot_path=shot)

    # ── Form filling ──────────────────────────────────────────────────────────

    def _do_fill_form(self) -> AdapterResult:
        """Multi-page form loop.

        Per page:
          1. If form fields visible → Vision field identification → deterministic fill
          2. Consent checkboxes
          3. Find Next / Submit button → click
          4. Repeat until Submit clicked or success detected or limit reached
        """
        from core.applicator import (
            _get_answers, _identify_fields, _fill_field,
            _check_consent_checkboxes, _generate_cover_letter,
            _screenshot as _app_screenshot, _has_visible_form,
            normalize_field_name, lookup_answer, _click_button,
            NEXT_BUTTON_TEXTS, SUBMIT_BUTTON_TEXTS,
        )

        page = self._page
        answers = _get_answers()
        job_id = self.job_hash[:8]

        # Generate cover letter once
        if not self._cover_letter:
            try:
                self._cover_letter = _generate_cover_letter(
                    self._client, self.job_title, self.company,
                    self.job_description,
                )
            except Exception as exc:
                logger.warning(f"Cover letter generation failed: {exc}")
                self._cover_letter = answers.get("about_me", "")

        answers["cover_letter"] = self._cover_letter

        cv_path = self._resolve_cv()
        last_shot: str | None = None

        for page_num in range(1, _MAX_FORM_PAGES + 1):
            logger.info(f"[{job_id}] Form page {page_num}")
            page.wait_for_timeout(1500)

            if not _has_visible_form(page):
                logger.info(f"[{job_id}] No form fields on page {page_num} — checking state")
                state = dom_detect_page_state(page)
                if state.kind == "success":
                    return AdapterResult.ok("success", screenshot_path=last_shot)
                if state.kind == "login":
                    return AdapterResult.ok("login", screenshot_path=last_shot)
                break

            shot_path = _app_screenshot(page, job_id, f"form_{page_num:02d}")
            last_shot = str(shot_path)
            self._screenshots.append(last_shot)

            # Vision field identification (only LLM call in this adapter)
            try:
                field_data = _identify_fields(self._client, shot_path)
                fields = field_data.get("fields", [])
            except Exception as exc:
                logger.warning(f"[{job_id}] Vision field identification failed: {exc}")
                fields = []

            # Fill each field deterministically
            for field in fields:
                try:
                    candidate_key = field.get("candidate_field", "")
                    label = field.get("label", "")
                    if not candidate_key:
                        candidate_key = normalize_field_name(label)
                    value = lookup_answer(candidate_key, answers, label, self._client)
                    if value:
                        _fill_field(page, field, value, answers, cv_path, 1)
                except Exception as exc:
                    logger.debug(f"[{job_id}] Field fill error ({field.get('label')}): {exc}")

            # Consent checkboxes
            _check_consent_checkboxes(page, 1)

            page.wait_for_timeout(500)

            # CAPTCHA check before navigating
            if _dom_detect_captcha(page):
                return AdapterResult.need_human(
                    "CAPTCHA detected during form filling",
                    screenshot_path=last_shot,
                )

            # Detect and click Next or Submit
            nav_result = self._click_navigation_button(
                page, field_data if fields else {}, job_id, page_num
            )

            if nav_result == "submit_clicked":
                page.wait_for_timeout(3000)
                shot_final = self._safe_screenshot("after_submit")
                if shot_final:
                    self._screenshots.append(shot_final)
                return AdapterResult.ok("submit", screenshot_path=shot_final,
                                        metadata={"pages_filled": page_num})

            if nav_result == "next_clicked":
                page.wait_for_timeout(2000)
                continue

            if nav_result == "no_button":
                # No navigation button — we might be on the last page already
                logger.info(f"[{job_id}] No navigation button — treating as submit page")
                return AdapterResult.ok("submit", screenshot_path=last_shot,
                                        metadata={"pages_filled": page_num})

        shot = self._safe_screenshot("form_limit_reached")
        return AdapterResult.fail(
            "failed",
            f"Reached form page limit ({_MAX_FORM_PAGES}) without submitting",
            screenshot_path=shot,
        )

    def _do_submit(self) -> AdapterResult:
        """Final submit step — click submit, confirm success."""
        from core.applicator import SUBMIT_BUTTON_TEXTS, _click_button

        page = self._page
        job_id = self.job_hash[:8]

        # Click submit button
        submitted = False
        for text in SUBMIT_BUTTON_TEXTS:
            try:
                loc = page.get_by_role("button", name=text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    if not self.auto_submit:
                        shot = self._safe_screenshot("pre_submit_paused")
                        return AdapterResult.fail(
                            "failed",
                            "auto_submit=False — stopping before final submit",
                            screenshot_path=shot,
                        )
                    loc.first.click()
                    page.wait_for_timeout(4000)
                    submitted = True
                    break
            except Exception:
                continue

        shot = self._safe_screenshot("after_submit_final")

        # Confirm success
        state = dom_detect_page_state(page)
        if state.kind == "success":
            return AdapterResult.ok("success", screenshot_path=shot,
                                    metadata={"submitted": submitted})

        # Vision fallback for success detection
        if shot:
            state2 = self._vision_classify_state(shot)
            if state2.kind == "success":
                return AdapterResult.ok("success", screenshot_path=shot,
                                        metadata={"submitted": submitted})

        if submitted:
            # Submitted but success not confirmed — optimistically treat as success
            logger.warning(f"[{job_id}] Submit clicked but success page not confirmed")
            return AdapterResult.ok("success", screenshot_path=shot,
                                    metadata={"submitted": True, "confirmed": False})

        return AdapterResult.fail("failed", "Submit button not found or submission failed",
                                  screenshot_path=shot)

    # ── Navigation helpers ────────────────────────────────────────────────────

    def _click_navigation_button(
        self, page: "Page", field_data: dict, job_id: str, page_num: int
    ) -> str:
        """Click Next or Submit. Returns 'next_clicked', 'submit_clicked', or 'no_button'."""
        from core.applicator import NEXT_BUTTON_TEXTS, SUBMIT_BUTTON_TEXTS

        # Vision told us what button is present — check that first
        vision_next = field_data.get("next_button", False)
        vision_submit = field_data.get("submit_button", False)
        vision_next_text = field_data.get("next_button_text", "")
        vision_submit_text = field_data.get("submit_button_text", "")

        candidate_texts = []
        if vision_next and vision_next_text:
            candidate_texts.append(("next", vision_next_text))
        if vision_submit and vision_submit_text:
            candidate_texts.append(("submit", vision_submit_text))
        for t in NEXT_BUTTON_TEXTS:
            candidate_texts.append(("next", t))
        for t in SUBMIT_BUTTON_TEXTS:
            candidate_texts.append(("submit", t))

        seen = set()
        for nav_type, text in candidate_texts:
            key = (nav_type, text.lower())
            if key in seen:
                continue
            seen.add(key)
            for role in ("button", "link"):
                try:
                    loc = page.get_by_role(role, name=text, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        page.wait_for_timeout(1000)
                        return "submit_clicked" if nav_type == "submit" else "next_clicked"
                except Exception:
                    continue

        return "no_button"

    def _click_apply_button(self, known_text: str | None = None) -> bool:
        """Click the Apply button. Returns True if clicked."""
        from core.applicator import APPLY_BUTTON_TEXTS

        page = self._page
        texts = ([known_text] if known_text else []) + APPLY_BUTTON_TEXTS

        for text in texts:
            for role in ("button", "link"):
                try:
                    loc = page.get_by_role(role, name=text, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        return True
                except Exception:
                    continue
        return False

    def _click_login_button(self) -> bool:
        """Click the login submit button."""
        from core.credential_manager import LOGIN_BUTTON_TEXTS

        page = self._page
        for text in LOGIN_BUTTON_TEXTS:
            for role in ("button",):
                try:
                    loc = page.get_by_role(role, name=text, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        return True
                except Exception:
                    continue
        # Fallback: any visible submit button
        try:
            loc = page.locator('button[type="submit"], input[type="submit"]')
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                return True
        except Exception:
            pass
        return False

    def _fill_input(self, selector: str, value: str, field_name: str) -> bool:
        """Fill the first visible input matching *selector*. Returns True if filled."""
        try:
            loc = self._page.locator(selector)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    el.fill(value)
                    logger.debug(f"[{self.job_hash[:8]}] Filled {field_name}")
                    return True
        except Exception as exc:
            logger.debug(f"_fill_input({field_name}): {exc}")
        return False

    def _dismiss_popups(self) -> None:
        """Close cookie banners / modals that might block the form."""
        dismiss = [
            'button:has-text("Accept")', 'button:has-text("Accept All")',
            'button:has-text("Got it")', 'button:has-text("Close")',
            'button:has-text("Dismiss")', '[aria-label="Close"]',
            '[aria-label="close"]', 'button.close',
            '#onetrust-accept-btn-handler',
        ]
        for sel in dismiss:
            try:
                loc = self._page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    self._page.wait_for_timeout(400)
                    return
            except Exception:
                continue

    def _safe_screenshot(self, name: str) -> str | None:
        """Take a screenshot without raising."""
        if not self._page:
            return None
        try:
            from config.settings import SCREENSHOTS_DIR
            shot_dir = SCREENSHOTS_DIR / self.job_hash[:8]
            shot_dir.mkdir(parents=True, exist_ok=True)
            path = shot_dir / f"{name}.png"
            self._page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:
            logger.debug(f"Screenshot failed ({name}): {exc}")
            return None

    def _vision_classify_state(self, screenshot_path: str) -> PageState:
        """Vision fallback for ambiguous page states.

        Only called when DOM analysis returns is_ambiguous=True.
        Maps Vision output back to a deterministic PageState.
        """
        try:
            from core.applicator import _ask_grok_vision_for_page_state
            from pathlib import Path
            result = _ask_grok_vision_for_page_state(self._client, Path(screenshot_path))
            status = result.get("status", "unknown")
            kind_map = {
                "success": "success", "error": "error",
                "login": "login", "signup": "signup",
                "2fa": "two_fa", "captcha": "captcha",
                "has_button": "apply_button", "unknown": "unknown",
            }
            kind = kind_map.get(status, "unknown")
            return PageState(
                kind=kind,
                is_ambiguous=(kind == "unknown"),
                apply_button_text=result.get("button_text"),
            )
        except Exception as exc:
            logger.debug(f"Vision state classification failed: {exc}")
            return PageState(kind="unknown", is_ambiguous=True)

    def _resolve_cv(self) -> "Path":
        from core.applicator import _resolve_cv_path
        return _resolve_cv_path(self.cv_variant)

    def _build_context(self) -> dict:
        """Build the context dict expected by BaseAdapter methods."""
        from core.applicator import _get_answers
        return {
            "job_hash": self.job_hash,
            "apply_url": self.apply_url,
            "job_title": self.job_title,
            "company": self.company,
            "job_description": self.job_description,
            "auto_submit": self.auto_submit,
            "cv_path": str(self._resolve_cv()),
            "answers": _get_answers(),
            "client": self._client,
        }


# ── Self-registration ──────────────────────────────────────────────────────────
register_adapter("generic", GenericAdapter)
