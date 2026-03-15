"""WorkdayAdapter — Workday ATS platform adapter.

Workday Flow
------------
  navigate → detect page state (Workday-specific DOM first)
  ├─ login page  → restore_session → login → [verify] → fill_form
  ├─ signup page → signup → [verify] → fill_form
  └─ form page   → fill_form (multi-step) → review → submit

Key selectors
-------------
Workday generates ``data-automation-id`` attributes from its internal component
framework.  These IDs are stable across ALL Workday tenants because they come
from the Workday platform, not individual employer configuration.

  Auth
    input[data-automation-id="email"]
    input[data-automation-id="password"]
    input[data-automation-id="verifyPassword"]
    button[data-automation-id="signInSubmitButton"]
    a[data-automation-id="createAccountLink"]
    button[data-automation-id="createAccountSubmitButton"]
    input[data-automation-id="agreed"]                         # consent

  Navigation
    button[data-automation-id="bottom-navigation-next-btn"]
    button[data-automation-id="bottom-navigation-previous-btn"]
    button[data-automation-id="bottom-navigation-save-continue-btn"]
    button[data-automation-id="submit-btn"]

  Form fields
    input[data-automation-id="legalNameSection_firstName"]
    input[data-automation-id="legalNameSection_lastName"]
    input[data-automation-id="phone-number"]
    input[data-automation-id="addressSection_addressLine1"]
    input[data-automation-id="addressSection_city"]
    input[data-automation-id="addressSection_postalCode"]
    input[data-automation-id="linkedin"]
    input[data-automation-id="website"]
    [data-automation-id="richTextEditor"] [contenteditable]    # cover letter
    input[type="file"]                                         # CV upload

  State detection
    div[data-automation-id="applicationSuccessPage"]           # success
    div[data-automation-id="applicationReviewPage"]            # review
    button[data-automation-id="useMyProfile"]                  # profile dialog
    button[data-automation-id="continue-application"]          # resume dialog

Session
-------
Playwright storage_state is encrypted and stored per-domain in the SessionStore
table.  A single Workday account covers all employer tenants on Workday ATS.
Session expires after 7 days (Workday's typical cookie TTL).

Vision
------
Used ONLY in plan() if DOM detection returns is_ambiguous=True.
Never drives navigation or form actions.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from openai import OpenAI

from core.adapters.base_adapter import (
    AdapterResult,
    PageState,
    dom_detect_page_state,
    _dom_detect_captcha,
    _visible,
)
from core.orchestrator import AdapterBase, ApplyState, StepResult, register_adapter

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page


# ── Constants ─────────────────────────────────────────────────────────────────

_PAGE_LOAD_TIMEOUT = 60_000
_WAIT_AFTER_CLICK  = 1_500   # ms
_WAIT_AFTER_NAV    = 2_500   # ms after navigation / page transition
_MAX_FORM_PAGES    = 20      # Workday can have 10+ pages

_PLATFORM_KEY = "workday"

WORKDAY_DOMAINS = ("myworkdayjobs.com", "myworkday.com", "myworkdaysite.com")


# ── Workday-specific selectors ─────────────────────────────────────────────────
# All data-automation-id selectors.  Grouped by feature area.

WD: dict[str, str] = {
    # ── Auth ──────────────────────────────────────────────────────────────────
    "email":           'input[data-automation-id="email"]',
    "password":        'input[data-automation-id="password"]',
    "verify_pwd":      'input[data-automation-id="verifyPassword"]',
    "sign_in":         'button[data-automation-id="signInSubmitButton"]',
    "create_account":  'a[data-automation-id="createAccountLink"]',
    "signup_submit":   'button[data-automation-id="createAccountSubmitButton"]',
    "agreed":          'input[data-automation-id="agreed"]',

    # ── Navigation ────────────────────────────────────────────────────────────
    "next_btn":   'button[data-automation-id="bottom-navigation-next-btn"]',
    "prev_btn":   'button[data-automation-id="bottom-navigation-previous-btn"]',
    "save_btn":   'button[data-automation-id="bottom-navigation-save-continue-btn"]',
    "submit_btn": 'button[data-automation-id="submit-btn"]',

    # ── Form fields ───────────────────────────────────────────────────────────
    "first_name":   'input[data-automation-id="legalNameSection_firstName"]',
    "last_name":    'input[data-automation-id="legalNameSection_lastName"]',
    "phone":        'input[data-automation-id="phone-number"]',
    "address_1":    'input[data-automation-id="addressSection_addressLine1"]',
    "city":         'input[data-automation-id="addressSection_city"]',
    "postal_code":  'input[data-automation-id="addressSection_postalCode"]',
    "linkedin_url": 'input[data-automation-id="linkedin"]',
    "website":      'input[data-automation-id="website"]',
    "rich_text":    '[data-automation-id="richTextEditor"] [contenteditable="true"]',
    "file_upload":  'input[type="file"]',

    # ── State detection ───────────────────────────────────────────────────────
    "success_page":  'div[data-automation-id="applicationSuccessPage"]',
    "review_page":   'div[data-automation-id="applicationReviewPage"]',
    "error_banner":  'div[data-automation-id="wd-Errors"]',

    # ── Profile / resume dialogs ──────────────────────────────────────────────
    "use_profile":   'button[data-automation-id="useMyProfile"]',
    "continue_app":  'button[data-automation-id="continue-application"]',
    "confirm_ok":    'button[data-automation-id="wd-CommandButton_uic_confirmButton"]',
}


# ── WorkdayAdapter ────────────────────────────────────────────────────────────

class WorkdayAdapter(AdapterBase):
    """Handles all Workday ATS job applications (myworkdayjobs.com, myworkday.com).

    Inherits AdapterBase (orchestrator.py) — the orchestration-level interface.
    Browser lifecycle is owned here: opened in plan(), closed in cleanup().

    One Workday account covers ALL employer tenants — credentials are stored
    under the single "workday" platform key in CompanyCredential.
    """

    name = "workday"

    @classmethod
    def detect(cls, url: str) -> bool:
        return any(d in url for d in WORKDAY_DOMAINS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._client: OpenAI | None = None
        self._screenshots: list[str] = []
        self._cover_letter: str | None = None

    # ── Orchestrator interface ────────────────────────────────────────────────

    def plan(self, checkpoint_meta: dict) -> StepResult:
        """Open browser, navigate to apply URL, detect initial page state."""
        try:
            self._open_browser()
            state = self._navigate_and_detect(self.apply_url, "01_plan")
            return self._page_state_to_step_result(state)
        except Exception as exc:
            logger.exception(f"[{self.job_hash[:8]}] WorkdayAdapter.plan failed: {exc}")
            return StepResult(ApplyState.FAILED, success=False, error=str(exc))

    def restore_session(self, checkpoint_meta: dict) -> StepResult:
        """Try saved Workday cookies; fall through to LOGIN if session is stale."""
        from core.credential_manager import load_session_state
        from urllib.parse import urlparse

        domain = urlparse(self.apply_url).hostname or ""
        session_json = load_session_state(domain)
        if not session_json:
            logger.info(f"[{self.job_hash[:8]}] No saved Workday session for {domain}")
            return StepResult(ApplyState.LOGIN)

        try:
            state_data = json.loads(session_json)
            self._context.add_cookies(state_data.get("cookies", []))
            self._page.reload(wait_until="networkidle", timeout=_PAGE_LOAD_TIMEOUT)
            self._page.wait_for_timeout(2_000)

            shot = self._safe_screenshot("session_restored")
            if shot:
                self._screenshots.append(shot)

            ps = self._detect_wd_state()
            if ps == "auth":
                logger.info(f"[{self.job_hash[:8]}] Workday session expired — need login")
                return StepResult(ApplyState.LOGIN, screenshot_path=shot)
            if ps == "form":
                logger.info(f"[{self.job_hash[:8]}] Workday session valid")
                return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)
            # success / unknown — proceed to form (navigate will handle it)
            return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)

        except Exception as exc:
            logger.warning(f"[{self.job_hash[:8]}] Session restore error: {exc}")
            return StepResult(ApplyState.LOGIN)

    def login(self, checkpoint_meta: dict) -> StepResult:
        """Fill Workday login form with stored credentials."""
        from core.credential_manager import (
            get_credential, mark_login_success, mark_account_status,
        )

        # Screenshot before touching the login form (baseline for debugging)
        before_shot = self._safe_screenshot("before_login")
        if before_shot:
            self._screenshots.append(before_shot)

        cred = get_credential(_PLATFORM_KEY)
        if not cred:
            logger.info(f"[{self.job_hash[:8]}] No Workday credentials stored — signing up")
            return StepResult(ApplyState.SIGNUP)

        email, password = cred

        # Ensure we're on the login page
        if not _visible(self._page, WD["sign_in"]):
            self._ensure_on_login_page()

        result = self._do_login(email, password)
        if result.screenshot_path:
            self._screenshots.append(result.screenshot_path)

        if result.requires_verification:
            mark_account_status(_PLATFORM_KEY, "pending_verification")
            return StepResult(
                ApplyState.VERIFY,
                meta={"platform_key": _PLATFORM_KEY, **result.metadata},
                screenshot_path=result.screenshot_path,
            )
        if result.success:
            mark_login_success(_PLATFORM_KEY)
            self._navigate_to_apply()
            return StepResult(
                ApplyState.FILL_FORM,
                screenshot_path=result.screenshot_path,
                meta=result.metadata,
            )
        logger.warning(f"[{self.job_hash[:8]}] Workday login failed: {result.error_message}")
        return StepResult(
            ApplyState.SIGNUP,
            success=False,
            error=result.error_message,
            screenshot_path=result.screenshot_path,
        )

    def signup(self, checkpoint_meta: dict) -> StepResult:
        """Create a new Workday account."""
        from core.credential_manager import (
            save_credential, generate_secure_password,
            PLATFORMS_NO_AUTO_SIGNUP,
        )
        from core.applicator import _get_answers

        if _PLATFORM_KEY in PLATFORMS_NO_AUTO_SIGNUP:
            return StepResult(
                ApplyState.HUMAN_INTERVENTION, success=False,
                error="Auto-signup disabled for Workday — manual login required",
            )

        answers = _get_answers()
        email = answers.get("email", os.environ.get("GMAIL_ADDRESS", ""))
        if not email:
            return StepResult(
                ApplyState.HUMAN_INTERVENTION, success=False,
                error="No email configured for Workday signup (check default_answers.yaml)",
            )
        password = generate_secure_password(20)

        result = self._do_signup(email, password)
        if result.screenshot_path:
            self._screenshots.append(result.screenshot_path)

        if result.success:
            save_credential(
                _PLATFORM_KEY, email, password,
                domain=self._page.url if self._page else "",
                auth_type="password",
            )
            if result.requires_verification:
                return StepResult(
                    ApplyState.VERIFY,
                    meta={"platform_key": _PLATFORM_KEY},
                    screenshot_path=result.screenshot_path,
                )
            self._navigate_to_apply()
            return StepResult(ApplyState.FILL_FORM, screenshot_path=result.screenshot_path)

        return StepResult(
            ApplyState.FAILED,
            success=False,
            error=result.error_message,
            screenshot_path=result.screenshot_path,
        )

    def verify(self, checkpoint_meta: dict) -> StepResult:
        """Handle Workday verification (email link or MFA OTP).

        Workday signup → always email-link based (no OTP on page).
        Workday MFA login → may show a numeric OTP field.

        Strategy:
        1. Check for Workday MFA OTP field → WhatsApp + poll
        2. Auto-verify via IMAP (email link)
        3. Fall back to HUMAN_INTERVENTION with WhatsApp notification
        """
        from core.verifier import (
            request_otp_from_user, poll_for_otp,
            request_human_intervention, clear_verification_state,
            VERIFY_TIMEOUT_SECONDS,
        )

        shot = self._safe_screenshot("wd_verify_page")
        if shot:
            self._screenshots.append(shot)

        page = self._page
        if not page:
            return StepResult(ApplyState.FAILED, success=False, error="No page in verify()")

        # ── 1. Workday MFA OTP field ───────────────────────────────────────────
        wd_otp_sel = (
            'input[data-automation-id="verificationCode"], '
            'input[autocomplete="one-time-code"]'
        )
        try:
            otp_loc = page.locator(wd_otp_sel)
            if otp_loc.count() > 0 and otp_loc.first.is_visible(timeout=2_000):
                request_otp_from_user(
                    self.job_hash, self.company, "Workday", self.apply_url
                )
                otp_code = poll_for_otp(self.job_hash, VERIFY_TIMEOUT_SECONDS)
                if not otp_code:
                    clear_verification_state()
                    return StepResult(
                        ApplyState.FAILED, success=False,
                        error="Workday OTP timeout — no user response",
                        screenshot_path=shot,
                    )
                try:
                    otp_loc.first.fill(otp_code)
                    verify_btn = page.locator(
                        'button[data-automation-id="verifyButton"], '
                        'button[type="submit"]'
                    )
                    if verify_btn.count() > 0:
                        verify_btn.first.click()
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception as exc:
                    return StepResult(ApplyState.FAILED, success=False,
                                      error=f"Workday OTP submit failed: {exc}")
                finally:
                    del otp_code

                self._navigate_to_apply()
                shot2 = self._safe_screenshot("wd_after_otp")
                if shot2:
                    self._screenshots.append(shot2)
                return StepResult(ApplyState.FILL_FORM, screenshot_path=shot2 or shot)
        except Exception as exc:
            logger.debug(f"[{self.job_hash[:8]}] Workday OTP check: {exc}")

        # ── 2. Auto-verify via IMAP ────────────────────────────────────────────
        try:
            from core.applicator import _auto_verify_email, _screenshot as _ashot

            result_inner: dict = {}
            ok, _ = _auto_verify_email(
                _PLATFORM_KEY, page, self._client,
                self.job_hash[:8], 1, result_inner,
            )
            if ok:
                self._navigate_to_apply()
                auto_shot = str(_ashot(page, self.job_hash[:8], "wd_verify_auto_done"))
                self._screenshots.append(auto_shot)
                logger.info(f"[{self.job_hash[:8]}] Workday email verified via IMAP")
                return StepResult(ApplyState.FILL_FORM, screenshot_path=auto_shot)
        except Exception as exc:
            logger.debug(f"[{self.job_hash[:8]}] IMAP auto-verify not available: {exc}")

        # ── 3. Email-link verification — user must act manually ────────────────
        request_human_intervention(
            self.job_hash, self.company, self.apply_url,
            "Workday email verification — click the link in your email, then send DONE",
        )
        return StepResult(
            ApplyState.HUMAN_INTERVENTION, success=False,
            error="Workday email verification required — waiting for user",
            screenshot_path=shot,
        )

    def fill_form(self, checkpoint_meta: dict) -> StepResult:
        """Multi-step Workday application form."""
        try:
            result = self._do_fill_workday_form()
            if result.screenshot_path:
                self._screenshots.append(result.screenshot_path)
            return StepResult(
                ApplyState(result.next_state),
                success=result.success,
                error=result.error_message,
                screenshot_path=result.screenshot_path,
                meta=result.metadata,
            )
        except Exception as exc:
            logger.exception(f"[{self.job_hash[:8]}] fill_form() crashed: {exc}")
            shot = self._safe_screenshot("fill_form_crash")
            return StepResult(ApplyState.FAILED, success=False,
                              error=str(exc), screenshot_path=shot)

    def review(self, checkpoint_meta: dict) -> StepResult:
        """Workday review page — verify we're there, then proceed to submit."""
        shot = self._safe_screenshot("review_page")
        if shot:
            self._screenshots.append(shot)
        if _visible(self._page, WD["review_page"]):
            logger.info(f"[{self.job_hash[:8]}] Workday review page confirmed")
        return StepResult(ApplyState.SUBMIT, screenshot_path=shot)

    def submit(self, checkpoint_meta: dict) -> StepResult:
        """Click Workday's submit button and confirm success."""
        page = self._page
        job_id = self.job_hash[:8]

        try:
            if not self.auto_submit:
                shot = self._safe_screenshot("pre_submit_paused")
                return StepResult(
                    ApplyState.FAILED, success=False,
                    error="auto_submit=False — stopped before Workday submit",
                    screenshot_path=shot,
                )

            submitted = False

            # Primary: Workday submit button
            if _visible(page, WD["submit_btn"]):
                page.locator(WD["submit_btn"]).first.click()
                page.wait_for_timeout(4_000)
                submitted = True
                logger.info(f"[{job_id}] Clicked Workday submit-btn")
            else:
                # Fallback: generic submit selectors
                for sel in (
                    'button[type="submit"]',
                    'button:has-text("Submit")',
                    'button:has-text("Apply")',
                ):
                    try:
                        loc = page.locator(sel)
                        if loc.count() > 0 and loc.first.is_visible():
                            loc.first.click()
                            page.wait_for_timeout(4_000)
                            submitted = True
                            logger.info(f"[{job_id}] Clicked fallback submit: {sel}")
                            break
                    except Exception:
                        continue

            # Handle confirmation dialog that Workday sometimes shows
            if _visible(page, WD["confirm_ok"]):
                try:
                    page.locator(WD["confirm_ok"]).first.click()
                    page.wait_for_timeout(3_000)
                except Exception:
                    pass

            shot = self._safe_screenshot("after_submit")

            # Primary success check: Workday-specific success page
            if _visible(page, WD["success_page"]):
                logger.info(f"[{job_id}] Workday applicationSuccessPage detected")
                return StepResult(ApplyState.SUCCESS, screenshot_path=shot,
                                  meta={"screenshots": self._screenshots})

            # Secondary: generic DOM success check (URL / class patterns)
            state = dom_detect_page_state(page)
            if state.kind == "success":
                return StepResult(ApplyState.SUCCESS, screenshot_path=shot,
                                  meta={"screenshots": self._screenshots})

            if submitted:
                # Submit was clicked but success page not confirmed — optimistically succeed
                logger.warning(f"[{job_id}] Submit clicked but success page not confirmed")
                return StepResult(
                    ApplyState.SUCCESS, screenshot_path=shot,
                    meta={"submitted": True, "confirmed": False,
                          "screenshots": self._screenshots},
                )

            return StepResult(
                ApplyState.FAILED, success=False,
                error="Workday submit button not found",
                screenshot_path=shot,
            )

        except Exception as exc:
            shot = self._safe_screenshot("submit_crash")
            return StepResult(ApplyState.FAILED, success=False,
                              error=str(exc), screenshot_path=shot)

    def cleanup(self, final_state: ApplyState, error: str | None) -> None:
        """Encrypt and save Workday session state, then close browser."""
        if self._page and self._browser:
            try:
                from core.credential_manager import save_session_state
                from urllib.parse import urlparse

                domain = urlparse(self.apply_url).hostname or ""
                storage_state = self._context.storage_state()
                save_session_state(
                    domain=domain,
                    platform_key=_PLATFORM_KEY,
                    storage_state=json.dumps(storage_state),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                )
            except Exception as exc:
                logger.debug(f"Workday session save skipped: {exc}")
            try:
                self._browser.close()
            except Exception:
                pass
            try:
                self._playwright.stop()
            except Exception:
                pass

        logger.info(
            f"[{self.job_hash[:8]}] WorkdayAdapter cleanup "
            f"final={final_state.value} err={error!r}"
        )

    # ── Browser lifecycle ─────────────────────────────────────────────────────

    def _open_browser(self) -> None:
        """Open Playwright browser — idempotent (no-op if already open)."""
        if self._page:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=False, slow_mo=150)
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._page = self._context.new_page()
        self._client = OpenAI(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        )

    def _navigate_and_detect(self, url: str, label: str) -> PageState:
        """Navigate to *url* and return a PageState using Workday-first DOM detection."""
        try:
            self._page.goto(url, wait_until="networkidle", timeout=_PAGE_LOAD_TIMEOUT)
        except Exception:
            self._page.goto(url, wait_until="load", timeout=_PAGE_LOAD_TIMEOUT)
        self._page.wait_for_timeout(_WAIT_AFTER_NAV)
        self._dismiss_popups()

        shot = self._safe_screenshot(label)
        if shot:
            self._screenshots.append(shot)

        # Workday-specific detection first; generic fallback if nothing matched
        state = self._wd_dom_detect()
        if state is None:
            state = dom_detect_page_state(self._page)
            if state.is_ambiguous and shot:
                state = self._vision_classify_state(shot)

        logger.info(
            f"[{self.job_hash[:8]}] Workday page state: kind={state.kind} "
            f"ambiguous={state.is_ambiguous}"
        )
        return state

    # ── Workday-specific DOM detection ────────────────────────────────────────

    def _wd_dom_detect(self) -> PageState | None:
        """Check Workday data-automation-id selectors.

        Returns a PageState when a Workday-specific state is unambiguously
        detected, or None to fall through to generic DOM detection.
        """
        page = self._page

        # Success
        if _visible(page, WD["success_page"]):
            return PageState(kind="success")

        # Auth — distinguish login from signup by presence of verify_pwd field
        if _visible(page, WD["email"]) and _visible(page, WD["password"]):
            if _visible(page, WD["verify_pwd"]):
                return PageState(kind="signup")
            return PageState(kind="login")

        # Signup via create-account link
        if _visible(page, WD["create_account"]):
            return PageState(kind="signup")

        # Review page
        if _visible(page, WD["review_page"]):
            return PageState(kind="form", details={"workday_review": True})

        # Active application form (any Workday nav button visible)
        if (
            _visible(page, WD["next_btn"])
            or _visible(page, WD["submit_btn"])
            or _visible(page, WD["save_btn"])
        ):
            return PageState(kind="form")

        return None  # fall through to generic detection

    def _detect_wd_state(self) -> str:
        """Classify current page as 'auth' | 'form' | 'success' | 'unknown'."""
        wd = self._wd_dom_detect() or dom_detect_page_state(self._page)
        if wd.kind == "success":
            return "success"
        if wd.kind in ("login", "signup", "two_fa"):
            return "auth"
        if wd.kind == "form":
            return "form"
        return "unknown"

    def _page_state_to_step_result(self, state: PageState) -> StepResult:
        """Map PageState to the correct next ApplyState."""
        shot = self._screenshots[-1] if self._screenshots else None
        kind = state.kind

        if kind == "success":
            return StepResult(ApplyState.SUCCESS, screenshot_path=shot)
        if kind == "captcha":
            return StepResult(ApplyState.HUMAN_INTERVENTION, success=False,
                              error="CAPTCHA on Workday landing page", screenshot_path=shot)
        if kind == "two_fa":
            return StepResult(ApplyState.VERIFY, screenshot_path=shot)
        if kind == "login":
            return StepResult(ApplyState.RESTORE_SESSION, screenshot_path=shot)
        if kind == "signup":
            return StepResult(ApplyState.SIGNUP, screenshot_path=shot)
        if kind == "form":
            if state.details.get("workday_review"):
                return StepResult(ApplyState.REVIEW, screenshot_path=shot)
            return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)
        if kind == "apply_button":
            clicked = self._click_apply_button(state.apply_button_text)
            if clicked:
                self._page.wait_for_timeout(_WAIT_AFTER_CLICK)
                shot2 = self._safe_screenshot("after_apply_click")
                if shot2:
                    self._screenshots.append(shot2)
                state2 = self._wd_dom_detect() or dom_detect_page_state(self._page)
                return self._page_state_to_step_result(state2)
            return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)

        # unknown — Vision fallback
        if shot:
            state2 = self._vision_classify_state(shot)
            if not state2.is_ambiguous:
                return self._page_state_to_step_result(state2)
        return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)

    # ── Navigation helpers ────────────────────────────────────────────────────

    def _ensure_on_login_page(self) -> None:
        """Navigate to apply URL and let Workday redirect to its login page."""
        try:
            self._page.goto(self.apply_url, wait_until="networkidle",
                            timeout=_PAGE_LOAD_TIMEOUT)
            self._page.wait_for_timeout(2_000)
        except Exception:
            pass

    def _navigate_to_apply(self) -> None:
        """Navigate back to apply URL after auth; handle post-login Workday dialogs."""
        try:
            self._page.goto(self.apply_url, wait_until="networkidle",
                            timeout=_PAGE_LOAD_TIMEOUT)
            self._page.wait_for_timeout(_WAIT_AFTER_NAV)
            self._dismiss_popups()
            self._handle_workday_dialogs()
        except Exception as exc:
            logger.debug(f"[{self.job_hash[:8]}] _navigate_to_apply: {exc}")

    def _handle_workday_dialogs(self) -> None:
        """Handle Workday post-login / mid-flow dialogs.

        Workday may show:
          - "Use My Profile" — pre-fill from saved Workday profile
          - "Continue Application" — resume a previous draft
          - Generic confirmation dialog
        """
        page = self._page

        for selector, label in (
            (WD["use_profile"],  "Use My Profile"),
            (WD["continue_app"], "Continue Application"),
            (WD["confirm_ok"],   "Confirm dialog"),
        ):
            if _visible(page, selector):
                try:
                    page.locator(selector).first.click()
                    page.wait_for_timeout(2_000)
                    logger.info(f"[{self.job_hash[:8]}] Clicked '{label}'")
                except Exception:
                    pass

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _do_login(self, email: str, password: str) -> AdapterResult:
        """Fill Workday login form using data-automation-id selectors."""
        page = self._page
        job_id = self.job_hash[:8]

        try:
            # Email
            if not self._fill_input(WD["email"], email, "email"):
                generic_email = (
                    'input[type="email"], input[autocomplete="username"], '
                    'input[name*="email" i]'
                )
                if not self._fill_input(generic_email, email, "email_generic"):
                    return AdapterResult.fail(
                        "signup", "Could not find email input on Workday login page"
                    )
            page.wait_for_timeout(500)

            # Password
            if not self._fill_input(WD["password"], password, "password"):
                if not self._fill_input('input[type="password"]', password, "password_generic"):
                    return AdapterResult.fail(
                        "failed", "Could not find password input on Workday login page"
                    )
            page.wait_for_timeout(300)

            # Sign In button
            if _visible(page, WD["sign_in"]):
                page.locator(WD["sign_in"]).first.click()
            elif _visible(page, 'button[type="submit"]'):
                page.locator('button[type="submit"]').first.click()
            else:
                return AdapterResult.fail("failed", "Could not find Workday Sign In button")

            page.wait_for_timeout(4_000)
            shot = self._safe_screenshot("after_wd_login")
            if shot:
                self._screenshots.append(shot)

            # Check body text for account / verification issues
            try:
                body = (page.locator("body").inner_text() or "").lower()
                if "account is not active" in body or "verify your email" in body:
                    return AdapterResult.need_verification(
                        screenshot_path=shot,
                        metadata={"reason": "email_not_verified"},
                    )
                if any(kw in body for kw in ("invalid", "incorrect", "wrong password",
                                              "user not found", "no account")):
                    logger.warning(f"[{job_id}] Workday login credentials rejected")
                    return AdapterResult.fail(
                        "signup", "Workday login credentials invalid — will try signup",
                        screenshot_path=shot,
                    )
            except Exception:
                pass

            ps = self._detect_wd_state()
            if ps == "auth":
                return AdapterResult.fail(
                    "signup", "Still on Workday auth page after login attempt",
                    screenshot_path=shot,
                )
            return AdapterResult.ok("fill_form", screenshot_path=shot)

        except Exception as exc:
            shot = self._safe_screenshot("wd_login_error")
            return AdapterResult.fail("failed", str(exc), screenshot_path=shot)

    def _do_signup(self, email: str, password: str) -> AdapterResult:
        """Create a new Workday account."""
        page = self._page
        job_id = self.job_hash[:8]

        try:
            # Start at apply URL — Workday will redirect to login page
            self._ensure_on_login_page()
            page.wait_for_timeout(2_000)

            # Click "Create Account" link
            if _visible(page, WD["create_account"]):
                page.locator(WD["create_account"]).first.click()
                page.wait_for_timeout(2_000)
            else:
                for text in ("Create Account", "Sign Up", "Register", "New User",
                             "Create an Account"):
                    for role in ("link", "button"):
                        try:
                            loc = page.get_by_role(role, name=text, exact=False)
                            if loc.count() > 0 and loc.first.is_visible():
                                loc.first.click()
                                page.wait_for_timeout(2_000)
                                break
                        except Exception:
                            continue

            shot = self._safe_screenshot("wd_signup_page")
            if shot:
                self._screenshots.append(shot)

            # Fill email
            self._fill_input(WD["email"], email, "email")
            page.wait_for_timeout(300)

            # Fill password and confirm
            self._fill_input(WD["password"], password, "password")
            page.wait_for_timeout(200)
            self._fill_input(WD["verify_pwd"], password, "verify_password")
            page.wait_for_timeout(300)

            # Consent checkbox
            if _visible(page, WD["agreed"]):
                try:
                    cb = page.locator(WD["agreed"]).first
                    if not cb.is_checked():
                        cb.check()
                        page.wait_for_timeout(200)
                except Exception:
                    pass

            page.wait_for_timeout(300)

            # Click signup submit
            if _visible(page, WD["signup_submit"]):
                page.locator(WD["signup_submit"]).first.click()
            else:
                for sel in (
                    'button[type="submit"]',
                    'button:has-text("Create Account")',
                    'button:has-text("Sign Up")',
                ):
                    try:
                        loc = page.locator(sel)
                        if loc.count() > 0 and loc.first.is_visible():
                            loc.first.click()
                            break
                    except Exception:
                        continue

            page.wait_for_timeout(4_000)
            shot = self._safe_screenshot("after_wd_signup")
            if shot:
                self._screenshots.append(shot)

            # Check for email verification prompt (most common Workday post-signup state)
            try:
                body = (page.locator("body").inner_text() or "").lower()
                if any(kw in body for kw in (
                    "verify your email", "check your email", "confirmation email",
                    "activation email", "we sent", "verification link",
                )):
                    logger.info(f"[{job_id}] Workday requires email verification")
                    return AdapterResult.need_verification(screenshot_path=shot)
            except Exception:
                pass

            ps = self._detect_wd_state()
            if ps == "form":
                return AdapterResult.ok("fill_form", screenshot_path=shot)
            if ps == "auth":
                return AdapterResult.fail(
                    "failed", "Workday signup failed — still on auth page",
                    screenshot_path=shot,
                )
            # Workday sometimes redirects directly to the application form
            return AdapterResult.ok("fill_form", screenshot_path=shot)

        except Exception as exc:
            shot = self._safe_screenshot("wd_signup_error")
            return AdapterResult.fail("failed", str(exc), screenshot_path=shot)

    # ── Form filling ──────────────────────────────────────────────────────────

    def _do_fill_workday_form(self) -> AdapterResult:
        """Multi-page Workday form filling.

        Per page:
          1. Check for terminal states (success, review, CAPTCHA)
          2. Fill known Workday fields by data-automation-id (deterministic)
          3. Vision field identification for remaining unknown fields
          4. Consent checkboxes
          5. Click Next / Save & Continue / Submit
        """
        from core.applicator import (
            _get_answers, _identify_fields, _fill_field,
            _check_consent_checkboxes, _generate_cover_letter,
            _screenshot as _app_screenshot,
            normalize_field_name, lookup_answer,
        )

        page = self._page
        answers = _get_answers()
        job_id = self.job_hash[:8]

        # Handle "Use My Profile" / "Continue Application" dialogs
        self._handle_workday_dialogs()
        page.wait_for_timeout(1_500)

        # Generate cover letter once
        if not self._cover_letter:
            try:
                self._cover_letter = _generate_cover_letter(
                    self._client, self.job_title, self.company, self.job_description,
                )
            except Exception as exc:
                logger.warning(f"[{job_id}] Cover letter generation failed: {exc}")
                self._cover_letter = answers.get("about_me", "")

        answers["cover_letter"] = self._cover_letter
        cv_path = self._resolve_cv()
        last_shot: str | None = None

        for page_num in range(1, _MAX_FORM_PAGES + 1):
            logger.info(f"[{job_id}] Workday form page {page_num}")
            page.wait_for_timeout(1_500)

            # Terminal state checks
            if _visible(page, WD["success_page"]):
                return AdapterResult.ok("success", screenshot_path=last_shot)

            if _visible(page, WD["review_page"]):
                return AdapterResult.ok("review", screenshot_path=last_shot,
                                        metadata={"pages_filled": page_num})

            if _dom_detect_captcha(page):
                return AdapterResult.need_human("CAPTCHA on Workday form",
                                                screenshot_path=last_shot)

            # Screenshot for Vision field identification
            shot_path = _app_screenshot(page, job_id, f"wd_form_{page_num:02d}")
            last_shot = str(shot_path)
            self._screenshots.append(last_shot)

            # ── Step 1: Fill known Workday fields (deterministic, data-automation-id) ─
            self._fill_known_workday_fields(answers, cv_path)

            # ── Step 2: Vision for remaining/unknown fields ─────────────────────────
            # Fields we already handle above — skip to avoid double-fill
            known_candidate_keys = {
                "first_name", "last_name", "phone", "email",
                "address", "city", "postal_code", "linkedin_url", "website",
                "cover_letter",
            }
            try:
                field_data = _identify_fields(self._client, shot_path)
                fields = field_data.get("fields", [])
            except Exception as exc:
                logger.warning(f"[{job_id}] Vision field ID failed: {exc}")
                field_data = {}
                fields = []

            for field in fields:
                try:
                    candidate_key = field.get("candidate_field", "")
                    label = field.get("label", "")
                    if not candidate_key:
                        candidate_key = normalize_field_name(label)
                    if candidate_key in known_candidate_keys:
                        continue
                    value = lookup_answer(candidate_key, answers, label, self._client)
                    if value:
                        _fill_field(page, field, value, answers, cv_path, 1)
                except Exception as exc:
                    logger.debug(f"[{job_id}] Vision field fill ({field.get('label')}): {exc}")

            # ── Step 3: Consent checkboxes ──────────────────────────────────────────
            _check_consent_checkboxes(page, 1)
            page.wait_for_timeout(500)

            # ── Step 4: Navigate to next page ───────────────────────────────────────
            nav = self._click_workday_nav()

            if nav == "submit_clicked":
                page.wait_for_timeout(4_000)
                shot = self._safe_screenshot("after_wd_submit_nav")
                if shot:
                    self._screenshots.append(shot)
                return AdapterResult.ok("submit", screenshot_path=shot,
                                        metadata={"pages_filled": page_num})

            if nav == "review_detected":
                return AdapterResult.ok("review", screenshot_path=last_shot,
                                        metadata={"pages_filled": page_num})

            if nav == "next_clicked":
                page.wait_for_timeout(2_500)
                continue

            if nav == "no_button":
                state = dom_detect_page_state(page)
                if state.kind == "success":
                    return AdapterResult.ok("success", screenshot_path=last_shot)
                return AdapterResult.ok("submit", screenshot_path=last_shot,
                                        metadata={"pages_filled": page_num})

        shot = self._safe_screenshot("wd_form_limit")
        return AdapterResult.fail(
            "failed",
            f"Reached Workday form page limit ({_MAX_FORM_PAGES}) without submitting",
            screenshot_path=shot,
        )

    def _fill_known_workday_fields(self, answers: dict, cv_path: "Path") -> None:
        """Fill Workday fields identifiable by stable data-automation-id selectors.

        Only fills fields that are currently visible and empty — never overwrites
        values the user may have already entered in their Workday profile.
        """
        page = self._page

        direct_mappings: dict[str, str] = {
            WD["first_name"]:   answers.get("first_name", ""),
            WD["last_name"]:    answers.get("last_name", ""),
            WD["phone"]:        answers.get("phone", ""),
            WD["address_1"]:    answers.get("address", ""),
            WD["city"]:         answers.get("city", ""),
            WD["postal_code"]:  answers.get("postal_code", ""),
            WD["linkedin_url"]: answers.get("linkedin_url", ""),
            WD["website"]:      answers.get("website", ""),
        }

        for selector, value in direct_mappings.items():
            if not value or not _visible(page, selector):
                continue
            try:
                el = page.locator(selector).first
                current = (el.input_value() or "").strip()
                if not current:
                    el.fill(value)
                    page.wait_for_timeout(150)
            except Exception as exc:
                logger.debug(f"Workday field ({selector[:50]}): {exc}")

        # Rich text editor — used for cover letter / motivation fields
        if _visible(page, WD["rich_text"]):
            try:
                el = page.locator(WD["rich_text"]).first
                current = (el.inner_text() or "").strip()
                if not current:
                    cover = answers.get("cover_letter", answers.get("about_me", ""))
                    if cover:
                        el.click()
                        el.fill(cover)
                        page.wait_for_timeout(300)
                        logger.debug(f"[{self.job_hash[:8]}] Filled Workday rich text field")
            except Exception as exc:
                logger.debug(f"Rich text fill: {exc}")

        # CV/resume file upload
        if _visible(page, WD["file_upload"]) and cv_path and cv_path.exists():
            try:
                fu = page.locator(WD["file_upload"]).first
                fu.set_input_files(str(cv_path))
                page.wait_for_timeout(2_000)
                logger.info(f"[{self.job_hash[:8]}] CV uploaded to Workday")
            except Exception as exc:
                logger.debug(f"CV upload: {exc}")

    def _click_workday_nav(self) -> str:
        """Click the active Workday navigation button.

        Priority:
          1. Submit button (last page) → check if review page first
          2. Next button
          3. Save & Continue button
          4. Generic button text fallback

        Returns: 'submit_clicked' | 'review_detected' | 'next_clicked' | 'no_button'
        """
        from core.applicator import NEXT_BUTTON_TEXTS, SUBMIT_BUTTON_TEXTS

        page = self._page

        # Submit button (last page of form) — but check for review first
        if _visible(page, WD["submit_btn"]):
            if _visible(page, WD["review_page"]):
                return "review_detected"
            page.locator(WD["submit_btn"]).first.click()
            page.wait_for_timeout(500)
            return "submit_clicked"

        # Next button
        if _visible(page, WD["next_btn"]):
            page.locator(WD["next_btn"]).first.click()
            page.wait_for_timeout(500)
            return "next_clicked"

        # Save & Continue button
        if _visible(page, WD["save_btn"]):
            page.locator(WD["save_btn"]).first.click()
            page.wait_for_timeout(500)
            return "next_clicked"

        # Generic text-based fallback
        for text in NEXT_BUTTON_TEXTS:
            try:
                loc = page.get_by_role("button", name=text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    page.wait_for_timeout(500)
                    return "next_clicked"
            except Exception:
                continue

        for text in SUBMIT_BUTTON_TEXTS:
            try:
                loc = page.get_by_role("button", name=text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    page.wait_for_timeout(500)
                    return "submit_clicked"
            except Exception:
                continue

        return "no_button"

    # ── Utility helpers ───────────────────────────────────────────────────────

    def _click_apply_button(self, known_text: str | None = None) -> bool:
        """Click the Apply button visible on a job listing page."""
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
        """Close cookie banners and modals that may block the form."""
        dismiss = [
            '#onetrust-accept-btn-handler',
            'button:has-text("Accept All")', 'button:has-text("Accept")',
            'button:has-text("Got it")', 'button:has-text("Close")',
            '[aria-label="Close"]', '[aria-label="close"]',
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
            path = shot_dir / f"wd_{name}.png"
            self._page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:
            logger.debug(f"Screenshot failed ({name}): {exc}")
            return None

    def _vision_classify_state(self, screenshot_path: str) -> PageState:
        """Vision fallback — only called when DOM detection is ambiguous."""
        try:
            from core.applicator import _ask_grok_vision_for_page_state
            result = _ask_grok_vision_for_page_state(self._client, Path(screenshot_path))
            status = result.get("status", "unknown")
            kind_map = {
                "success": "success", "error": "error",
                "login": "login", "signup": "signup",
                "2fa": "two_fa", "captcha": "captcha",
                "has_button": "apply_button", "unknown": "unknown",
            }
            kind = kind_map.get(status, "unknown")
            return PageState(kind=kind, is_ambiguous=(kind == "unknown"),
                             apply_button_text=result.get("button_text"))
        except Exception as exc:
            logger.debug(f"Vision classify failed: {exc}")
            return PageState(kind="unknown", is_ambiguous=True)

    def _resolve_cv(self) -> "Path":
        from core.applicator import _resolve_cv_path
        return _resolve_cv_path(self.cv_variant)


# ── Self-registration ──────────────────────────────────────────────────────────
register_adapter("workday", WorkdayAdapter)
