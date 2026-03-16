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
import re
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
    "phone_alt":    'input[data-automation-id="phoneNumber"]',
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
            try:
                page.wait_for_timeout(1_500)
            except Exception:
                # Page was closed/navigated — likely submitted successfully
                logger.info(f"[{job_id}] Page closed at page {page_num} — treating as submit")
                return AdapterResult.ok("submit",
                                        metadata={"pages_filled": page_num - 1,
                                                  "submitted": True})

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
                # Binary yes/no radios — handled by _default_unfilled_binary_radios (Step 1+4)
                # Prevent Vision from overriding with wrong "yes" answers
                "previously_employed", "candidateispreviousworker",
                "former_employee", "previous_employment",
                # work_authorization: Vision misidentifies company-specific yes/no questions
                # (e.g. "previously employed here?") as work_authorization → answers "Yes"
                "work_authorization", "authorized_to_work", "legally_authorized",
                # visa sponsorship — handled by _fill_wd_select_by_label → "No"
                "visa_sponsorship_required", "require_sponsorship",
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
                    normalized_label = normalize_field_name(label)
                    if candidate_key in known_candidate_keys or normalized_label in known_candidate_keys:
                        continue
                    value = lookup_answer(label, candidate_key, field.get("type", "text"))
                    if value:
                        _fill_field(page, field, value, 1, self._cover_letter or "")
                except Exception as exc:
                    logger.debug(f"[{job_id}] Vision field fill ({field.get('label')}): {exc}")

            # ── Step 3: Uncheck optional Workday toggles that open extra sections ──────
            for opt_label in ("I have a preferred name",):
                try:
                    loc = page.get_by_label(opt_label, exact=False)
                    if loc.count() > 0 and loc.first.is_visible() and loc.first.is_checked():
                        loc.first.uncheck()
                        page.wait_for_timeout(300)
                        logger.debug(f"[{job_id}] Unchecked optional: {opt_label!r}")
                except Exception:
                    pass

            # ── Step 4: Binary yes/no radio defaults + consent checkboxes ────────────
            # force=True overrides any "Yes" that Vision may have incorrectly set
            self._default_unfilled_binary_radios(page, job_id, force=True)
            _check_consent_checkboxes(page, 1)
            page.wait_for_timeout(500)

            # ── Step 4: Navigate to next page ───────────────────────────────────────
            nav = self._click_workday_nav()

            if nav == "submit_clicked":
                try:
                    page.wait_for_timeout(4_000)
                except Exception:
                    # Page navigated away after submit — this is normal for successful submission
                    logger.info(f"[{job_id}] Page closed after submit — treating as success")
                    return AdapterResult.ok("submit",
                                            metadata={"pages_filled": page_num,
                                                      "submitted": True})
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
                # Check for validation errors and retry up to 3 times
                for _retry in range(3):
                    errors = self._get_validation_errors()
                    if not errors:
                        break
                    shot_err = self._safe_screenshot(f"wd_err_{page_num:02d}_r{_retry}")
                    if shot_err:
                        self._screenshots.append(shot_err)
                    filled = self._handle_validation_errors(errors, answers)
                    if not filled:
                        # Cannot fill missing fields — stop retrying
                        logger.warning(f"[{job_id}] Could not fill validation error fields: {errors}")
                        break
                    page.wait_for_timeout(500)
                    nav = self._click_workday_nav()
                    page.wait_for_timeout(1_500)
                    if nav not in ("next_clicked",):
                        break
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

    def _default_unfilled_binary_radios(self, page: "Page", job_id: str,
                                          force: bool = False) -> None:
        """For any required radio group that has only two options (Yes / No),
        default to 'No' if no option is currently selected.

        When ``force=True`` (called after Vision), also overrides any group
        where 'Yes' is currently checked — Vision sometimes misidentifies
        company-specific questions (e.g. "previously employed here?") and
        clicks 'Yes'.  Forcing 'No' after Vision ensures those questions are
        answered correctly.

        This handles company-specific yes/no questions like
        "Have you previously been employed here?" without needing them in
        default_answers.yaml.
        """
        try:
            # Find all visible radio inputs, group by name attribute
            radios = page.locator('input[type="radio"]')
            count = radios.count()
            groups: dict[str, list] = {}
            for i in range(count):
                try:
                    r = radios.nth(i)
                    if not r.is_visible():
                        continue
                    name = r.get_attribute("name") or f"__unnamed_{i}"
                    groups.setdefault(name, []).append(r)
                except Exception:
                    continue

            for name, members in groups.items():
                # Only process binary (2-option) groups
                if len(members) != 2:
                    continue

                # Determine current state
                checked_labels = []
                for m in members:
                    try:
                        mid = m.get_attribute("id")
                        lbl_text = ""
                        if mid:
                            lbl_el = page.locator(f'label[for="{mid}"]')
                            if lbl_el.count() > 0:
                                lbl_text = (lbl_el.first.inner_text() or "").strip().lower()
                        val = (m.get_attribute("value") or "").lower()
                        if m.is_checked():
                            checked_labels.append(lbl_text or val)
                    except Exception:
                        continue

                # Skip if already "No" (correct answer)
                no_already = any("no" in lbl or lbl in ("no", "false") for lbl in checked_labels)
                if no_already:
                    continue

                # Skip if something OTHER than yes/no is checked and force=False
                yes_checked = any("yes" in lbl or lbl in ("yes", "true") for lbl in checked_labels)
                nothing_checked = len(checked_labels) == 0
                if not force and not nothing_checked:
                    continue

                # Find and click the "No" option
                for m in members:
                    try:
                        lbl_text = ""
                        mid = m.get_attribute("id")
                        if mid:
                            lbl_el = page.locator(f'label[for="{mid}"]')
                            if lbl_el.count() > 0:
                                lbl_text = (lbl_el.first.inner_text() or "").strip().lower()
                        val = (m.get_attribute("value") or "").lower()
                        if "no" in lbl_text or val == "no" or val == "false":
                            m.check()
                            action = "forced" if yes_checked else "defaulted"
                            logger.debug(f"[{job_id}] Binary radio {action} → 'No' (name={name})")
                            break
                    except Exception:
                        continue
        except Exception as exc:
            logger.debug(f"[{job_id}] _default_unfilled_binary_radios: {exc}")

    def _fill_known_workday_fields(self, answers: dict, cv_path: "Path") -> None:
        """Fill Workday fields identifiable by stable data-automation-id selectors.

        Only fills fields that are currently visible and empty — never overwrites
        values the user may have already entered in their Workday profile.
        """
        page = self._page

        direct_mappings: dict[str, str] = {
            WD["first_name"]:   answers.get("first_name", ""),
            WD["last_name"]:    answers.get("last_name", ""),
            WD["email"]:        answers.get("email", ""),
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

        # Latin-script name fields (Workday international tenants use different automation-IDs)
        for label_text, ans_key in [
            ("Given Name(s) - Latin Script", "first_name"),
            ("Family Name - Latin Script", "last_name"),
        ]:
            val = answers.get(ans_key, "")
            if not val:
                continue
            try:
                loc = page.get_by_label(label_text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    el = loc.first
                    current = (el.input_value() or "").strip()
                    if not current or (ans_key == "first_name" and " " in current):
                        el.fill(val)
                        page.wait_for_timeout(150)
                        logger.debug(f"[{self.job_hash[:8]}] Filled {label_text!r} → {val!r}")
            except Exception as exc:
                logger.debug(f"Latin name ({label_text}): {exc}")

        # Email — try multiple automation-IDs, then label fallback with tag check
        email_val = answers.get("email", "")
        if email_val:
            _filled_email = False
            for _esel in (
                WD["email"],
                'input[data-automation-id="emailAddress"]',
                'input[data-automation-id="email-address"]',
                'input[type="email"]',
                'input[data-automation-id*="mail"]',
            ):
                if _visible(page, _esel):
                    try:
                        el = page.locator(_esel).first
                        cur = ""
                        try:
                            cur = (el.input_value() or "").strip()
                        except Exception:
                            pass
                        if not cur:
                            el.fill(email_val)
                            page.wait_for_timeout(150)
                            logger.debug(f"[{self.job_hash[:8]}] Filled Email ({_esel})")
                        _filled_email = True
                        break
                    except Exception as exc:
                        logger.debug(f"Email fill ({_esel}): {exc}")
            if not _filled_email:
                # Label-based fallback — find any visible input labelled "Email"
                for lbl_hint in ("Email Address", "Email"):
                    try:
                        loc = page.get_by_label(lbl_hint, exact=False)
                        for _i in range(min(loc.count(), 3)):
                            el = loc.nth(_i)
                            if not el.is_visible():
                                continue
                            try:
                                tag = el.evaluate("el => el.tagName.toLowerCase()")
                            except Exception:
                                continue
                            if tag != "input":
                                continue
                            try:
                                cur = (el.input_value() or "").strip()
                            except Exception:
                                cur = ""
                            if not cur:
                                el.fill(email_val)
                                page.wait_for_timeout(150)
                                logger.debug(f"[{self.job_hash[:8]}] Filled Email ({lbl_hint!r} label)")
                            _filled_email = True
                            break
                        if _filled_email:
                            break
                    except Exception as exc:
                        logger.debug(f"Email label fallback ({lbl_hint!r}): {exc}")

        # Phone number fallback if direct automation-id not visible
        phone_val = answers.get("phone", "")
        if phone_val and not _visible(page, WD["phone"]):
            # Try alternate automation-id first
            if _visible(page, WD["phone_alt"]):
                try:
                    el = page.locator(WD["phone_alt"]).first
                    if not (el.input_value() or "").strip():
                        el.fill(phone_val)
                        page.wait_for_timeout(150)
                        logger.debug(f"[{self.job_hash[:8]}] Filled Phone Number (phoneNumber id)")
                except Exception as exc:
                    logger.debug(f"Phone phone_alt fill: {exc}")
            else:
                try:
                    loc = page.get_by_label("Phone Number", exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        el = loc.first
                        tag = el.evaluate("el => el.tagName.toLowerCase()")
                        if tag == "input" and not (el.input_value() or "").strip():
                            el.fill(phone_val)
                            page.wait_for_timeout(150)
                            logger.debug(f"[{self.job_hash[:8]}] Filled Phone Number (label fallback)")
                except Exception as exc:
                    logger.debug(f"Phone number fallback: {exc}")

        # Workday combobox dropdowns — click-based approach (custom React components)
        job_id = self.job_hash[:8]
        self._fill_wd_select_by_label(page, "How Did You Hear About Us", "LinkedIn", job_id)
        self._fill_wd_select_by_label(page, "Phone Device Type", "Mobile", job_id)
        self._fill_wd_select_by_label(page, "Country", "Israel", job_id)
        # Application Questions page comboboxes
        self._fill_wd_select_by_label(page, "require visa sponsorship", "No", job_id)
        self._fill_wd_select_by_label(page, "eligible to work in the country", "Yes", job_id)
        self._fill_wd_select_by_label(page, "non-compete or non-solicitation", "No", job_id)

        # Education section (My Experience page) — fill inline form
        self._fill_workday_education(page, job_id, answers)

        # Binary yes/no radios: default all unchecked groups to "No" BEFORE Vision runs.
        # This prevents Vision from incorrectly clicking "Yes" on company-specific questions.
        self._default_unfilled_binary_radios(page, job_id)

    def _fill_workday_education(self, page: "Page", job_id: str, answers: dict) -> None:
        """Fill the Education section on the My Experience page.

        Workday shows Education as an inline form (no separate Save button).
        Fields are saved automatically when Next is clicked.

        Strategy: find "Education" as a leaf text node, then find the first "Add"
        button that appears AFTER it in DOM order (and before Resume/CV section).
        Guard: if School input is already visible, the form is already open — just fill.
        """
        try:
            # Guard: if education fields already visible, just fill them (no Add needed)
            school_inp = page.locator('input[data-automation-id*="school" i]').first
            edu_already_open = school_inp.count() > 0 and school_inp.is_visible()

            if not edu_already_open:
                # Find Add button that comes AFTER "Education" heading AND BEFORE "Resume/CV"
                add_btn_idx = page.evaluate("""() => {
                    const allEls = Array.from(document.body.querySelectorAll('*'));
                    const addBtns = allEls.filter(
                        e => e.tagName === 'BUTTON' &&
                             e.textContent.trim() === 'Add' &&
                             e.getBoundingClientRect().width > 0
                    );
                    if (addBtns.length === 0) return -1;

                    // Find the "Education" leaf text node (exact text, not containing other elements)
                    let eduIdx = -1;
                    for (let i = 0; i < allEls.length; i++) {
                        const el = allEls[i];
                        if (el.children.length === 0 &&
                            el.textContent.trim() === 'Education' &&
                            el.getBoundingClientRect().height > 0) {
                            eduIdx = i;
                            break;
                        }
                    }
                    if (eduIdx < 0) return -1;

                    // Find first Add button AFTER the Education node,
                    // stopping if we hit a Resume/CV or Websites section
                    for (let i = 0; i < addBtns.length; i++) {
                        const btnIdx = allEls.indexOf(addBtns[i]);
                        if (btnIdx <= eduIdx) continue;  // before Education
                        // Verify nothing like "Resume/CV" is between Education and this button
                        let blocked = false;
                        for (let j = eduIdx + 1; j < btnIdx; j++) {
                            const t = allEls[j].textContent.trim();
                            if (t === 'Resume/CV' || t === 'Websites') {
                                blocked = true; break;
                            }
                        }
                        if (!blocked) return i;
                    }
                    return -1;
                }""")

                if add_btn_idx is None or add_btn_idx < 0:
                    return

                add_btns = page.locator('button').filter(has_text=re.compile(r'^Add$'))
                if add_btn_idx >= add_btns.count():
                    return
                add_btn = add_btns.nth(add_btn_idx)
                if not add_btn.is_visible():
                    return

                add_btn.click()
                page.wait_for_timeout(1000)
                logger.debug(f"[{job_id}] Education: clicked Add")

            # Fill School Name (Workday autocomplete text input)
            school = answers.get("university", "Bar Ilan University")
            for sel in (
                'input[data-automation-id*="school" i]',
                'input[data-automation-id*="institution" i]',
            ):
                locs = page.locator(sel)
                if locs.count() > 0 and locs.first.is_visible():
                    cur = (locs.first.input_value() or "").strip()
                    if not cur:
                        locs.first.fill(school)
                        page.wait_for_timeout(600)
                        opt = page.locator('[data-automation-id="promptOption"]').first
                        if opt.is_visible():
                            opt.click()
                            page.wait_for_timeout(400)
                        logger.debug(f"[{job_id}] Education: filled school")
                    break

            # Fill Degree — use "Bachelor" as keyword (Workday uses "Bachelor of Science" etc.)
            self._fill_wd_select_by_label(page, "Degree", "Bachelor", job_id)

            # Fill Field of Study / Major
            field = answers.get("field_of_study", "Software Engineering")
            for lbl in ("Field of Study", "Major", "Concentration"):
                try:
                    inp = page.get_by_label(lbl, exact=False).first
                    if inp.is_visible():
                        tag = inp.evaluate("el => el.tagName.toLowerCase()")
                        if tag == "input":
                            cur = (inp.input_value() or "").strip()
                            if not cur:
                                inp.fill(field)
                                page.wait_for_timeout(300)
                            break
                except Exception:
                    pass

            # No Save button — inline form is saved when Next is clicked
            logger.debug(f"[{job_id}] Education: fields filled (inline, saved on Next)")
        except Exception as exc:
            logger.debug(f"[{job_id}] Education fill: {exc}")

    def _fill_wd_select_by_label(self, page: "Page", label_hint: str,
                                  preferred: str, job_id: str) -> bool:
        """Fill a Workday combobox/dropdown by clicking it open and selecting an option.

        Workday renders dropdowns as custom React combobox components, NOT native
        <select> elements.  The correct approach:
          1. Click the combobox trigger to open the dropdown
          2. Wait for [data-automation-id="promptOption"] items to appear
          3. Click the matching option

        Tries multiple strategies to locate the trigger element.
        """
        def _pick_option(pref: str) -> bool:
            """After the dropdown is open, click the best matching option.

            Only considers VISIBLE options — Workday pre-loads options in the DOM
            (hidden) so count() > 0 even when the dropdown is closed.
            """
            # Wait up to 2s for at least one visible promptOption to appear
            try:
                page.wait_for_selector(
                    '[data-automation-id="promptOption"]',
                    state="visible", timeout=2000,
                )
            except Exception:
                pass  # Fallback to role-based selectors below

            for opt_sel in (
                '[data-automation-id="promptOption"]',
                '[role="option"]',
                '[role="listitem"]',
            ):
                opts = page.locator(opt_sel)
                count = opts.count()
                if count == 0:
                    continue
                # First pass: startswith match — prevents "No" from matching "Yes...non-compete..."
                for i in range(count):
                    try:
                        opt = opts.nth(i)
                        if not opt.is_visible():
                            continue
                        t = (opt.inner_text() or "").strip()
                        if t.lower().startswith(pref.lower()):
                            opt.click()
                            page.wait_for_timeout(400)
                            return True
                    except Exception:
                        continue
                # Second pass: substring match
                for i in range(count):
                    try:
                        opt = opts.nth(i)
                        if not opt.is_visible():
                            continue
                        if pref.lower() in (opt.inner_text() or "").lower():
                            opt.click()
                            page.wait_for_timeout(400)
                            return True
                    except Exception:
                        continue
                # Fallback: first visible non-placeholder
                for i in range(count):
                    try:
                        opt = opts.nth(i)
                        if not opt.is_visible():
                            continue
                        t = (opt.inner_text() or "").strip().lower()
                        if t and not t.startswith("select") and t != "--":
                            opt.click()
                            page.wait_for_timeout(400)
                            return True
                    except Exception:
                        continue
            return False

        hint_lower = label_hint.lower()

        def _try_trigger(trigger) -> bool:
            """Click trigger, wait for visible options, click best match, verify."""
            try:
                # Check if already showing preferred value
                try:
                    cur = (trigger.inner_text() or "").strip()
                    if preferred.lower() in cur.lower() and "select" not in cur.lower():
                        logger.debug(f"[{job_id}] {label_hint!r} already = {preferred!r}")
                        return True
                except Exception:
                    pass

                trigger.scroll_into_view_if_needed()
                trigger.click()
                page.wait_for_timeout(700)

                if _pick_option(preferred):
                    # Verify the trigger now shows the selected value
                    page.wait_for_timeout(300)
                    try:
                        new_text = (trigger.inner_text() or "").strip()
                        if preferred.lower() in new_text.lower():
                            logger.info(f"[{job_id}] WD combobox {label_hint!r} → {preferred!r}")
                            return True
                        # Selection reverted — try keyboard approach
                        logger.debug(f"[{job_id}] {label_hint!r} click reverted ({new_text!r}), trying keyboard")
                    except Exception:
                        logger.info(f"[{job_id}] WD combobox {label_hint!r} → {preferred!r}")
                        return True

                # Keyboard fallback: close any open dropdown, reopen, type to filter
                try:
                    page.keyboard.press("Escape")   # close if open (toggle-safe)
                    page.wait_for_timeout(200)
                    trigger.scroll_into_view_if_needed()
                    trigger.click()
                    page.wait_for_timeout(600)
                    page.keyboard.type(preferred[:4], delay=60)
                    page.wait_for_timeout(700)
                    opt = page.locator('[data-automation-id="promptOption"]').first
                    if opt.is_visible():
                        opt.click()
                        page.wait_for_timeout(400)
                        try:
                            new_text = (trigger.inner_text() or "").strip()
                            if preferred.lower() in new_text.lower():
                                logger.info(f"[{job_id}] WD combobox {label_hint!r} → {preferred!r} (kbd)")
                                return True
                        except Exception:
                            pass
                except Exception:
                    pass
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
            except Exception as exc:
                logger.debug(f"[{job_id}] _try_trigger({label_hint!r}): {exc}")
            return False

        # Strategy 1: formField container → native <select> or comboboxButton inside
        # Workday wraps each field in a [data-automation-id^="formField"] section.
        # Application Questions may use native <select> elements (not comboboxButton).
        try:
            # 1a: strict label filter (My Information page)
            containers = page.locator('[data-automation-id^="formField"]').filter(
                has=page.locator(f'label:has-text("{label_hint}")')
            )
            cnt = containers.count()
            # 1b: text-content filter (Application Questions & other pages without <label>)
            if cnt == 0:
                containers = page.locator('[data-automation-id^="formField"]').filter(
                    has_text=label_hint
                )
                cnt = containers.count()
            logger.debug(f"[{job_id}] WD S1 {label_hint!r}: {cnt} containers")
            if cnt > 0:
                c = containers.first
                # 1c: try native <select> first (Application Questions page)
                sel_el = c.locator('select')
                if sel_el.count() > 0 and sel_el.first.is_visible():
                    try:
                        sel_el.first.select_option(label=preferred)
                        page.wait_for_timeout(300)
                        logger.debug(f"[{job_id}] WD S1 native select → {preferred!r}")
                        return True
                    except Exception:
                        try:
                            sel_el.first.select_option(value=preferred.lower())
                            page.wait_for_timeout(300)
                            logger.debug(f"[{job_id}] WD S1 native select (value) → {preferred!r}")
                            return True
                        except Exception as exc2:
                            logger.debug(f"[{job_id}] WD S1 native select failed: {exc2}")
                # 1d: try comboboxButton (My Information page custom comboboxes)
                combo_btn = c.locator('[data-automation-id="comboboxButton"]')
                btn = combo_btn.first if combo_btn.count() > 0 else c.locator('button').first
                vis = btn.is_visible()
                logger.debug(f"[{job_id}] WD S1 btn visible={vis}")
                if vis:
                    if _try_trigger(btn):
                        return True
        except Exception as exc:
            logger.debug(f"[{job_id}] WD combobox container ({label_hint!r}): {exc}")

        # Strategy 2: get_by_label → click element directly
        try:
            loc = page.get_by_label(label_hint, exact=False)
            lcnt = loc.count()
            logger.debug(f"[{job_id}] WD S2 {label_hint!r}: {lcnt} by-label")
            for i in range(min(lcnt, 3)):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                if _try_trigger(el):
                    return True
        except Exception as exc:
            logger.debug(f"[{job_id}] WD combobox label ({label_hint!r}): {exc}")

        # Strategy 3: JS — find comboboxButton near ANY text element containing hint
        # Handles Application Questions which use <div>/<p> instead of <label>
        try:
            idx = page.evaluate(
                """(hint) => {
                    hint = hint.toLowerCase();
                    // Search label, then any visible text-bearing element
                    const selectors = ['label', 'p', 'div[class*="label" i]',
                                       'div[data-automation-id*="label" i]', 'span'];
                    for (const sel of selectors) {
                        for (const lbl of document.querySelectorAll(sel)) {
                            if (!lbl.textContent.toLowerCase().includes(hint)) continue;
                            if (lbl.getBoundingClientRect().height === 0) continue;
                            let el = lbl.parentElement;
                            for (let i = 0; i < 8; i++) {
                                if (!el) break;
                                // Prefer comboboxButton
                                const combo = el.querySelector('[data-automation-id="comboboxButton"]');
                                if (combo && combo.getBoundingClientRect().width > 0) {
                                    const all = Array.from(document.querySelectorAll('[data-automation-id="comboboxButton"]'));
                                    return all.indexOf(combo);
                                }
                                const btn = el.querySelector('button');
                                if (btn && btn.getBoundingClientRect().width > 0) {
                                    const allBtns = Array.from(document.querySelectorAll('[data-automation-id="comboboxButton"]'));
                                    const cb = el.querySelector('[data-automation-id="comboboxButton"]');
                                    if (cb) return allBtns.indexOf(cb);
                                }
                                el = el.parentElement;
                            }
                        }
                    }
                    return -1;
                }""",
                hint_lower,
            )
            if idx is not None and idx >= 0:
                btn = page.locator('[data-automation-id="comboboxButton"]').nth(idx)
                if btn.is_visible():
                    if _try_trigger(btn):
                        return True
        except Exception as exc:
            logger.debug(f"[{job_id}] WD combobox JS ({label_hint!r}): {exc}")

        # Strategy 4: JS — find native <select> near text matching hint
        # Catches Application Questions dropdowns not inside formField containers
        try:
            idx4 = page.evaluate(
                """(hint) => {
                    hint = hint.toLowerCase();
                    const allSelects = Array.from(document.querySelectorAll('select'));
                    for (const sel of allSelects) {
                        if (sel.getBoundingClientRect().width === 0) continue;
                        let el = sel.parentElement;
                        for (let i = 0; i < 10; i++) {
                            if (!el) break;
                            if (el.textContent.toLowerCase().includes(hint)) {
                                return allSelects.indexOf(sel);
                            }
                            el = el.parentElement;
                        }
                    }
                    return -1;
                }""",
                hint_lower,
            )
            if idx4 is not None and idx4 >= 0:
                sel = page.locator('select').nth(idx4)
                if sel.is_visible():
                    try:
                        sel.select_option(label=preferred)
                        page.wait_for_timeout(300)
                        logger.debug(f"[{job_id}] WD S4 native select → {preferred!r}")
                        return True
                    except Exception:
                        try:
                            sel.select_option(value=preferred.lower())
                            page.wait_for_timeout(300)
                            logger.debug(f"[{job_id}] WD S4 native select (value) → {preferred!r}")
                            return True
                        except Exception as exc4:
                            logger.debug(f"[{job_id}] WD S4 native select failed: {exc4}")
        except Exception as exc:
            logger.debug(f"[{job_id}] WD native select JS ({label_hint!r}): {exc}")
        return False

    def _get_validation_errors(self) -> list[str]:
        """Return error messages from the Workday validation error banner.

        Workday renders validation errors inside:
          div[data-automation-id="wd-Errors"]

        Each required-field error has a predictable format, e.g.:
          "Given Name(s) - Latin Script is required"
          "Phone Number is required"
        """
        page = self._page
        errors: list[str] = []
        try:
            banner = page.locator(WD["error_banner"])
            if banner.count() > 0 and banner.first.is_visible(timeout=1_000):
                raw = (banner.first.inner_text() or "").strip()
                for line in re.split(r"[\n•\-]+", raw):
                    line = line.strip()
                    if line:
                        errors.append(line)
        except Exception as exc:
            logger.debug(f"[{self.job_hash[:8]}] _get_validation_errors: {exc}")
        if errors:
            logger.warning(f"[{self.job_hash[:8]}] Validation errors: {errors}")
        return errors

    def _handle_validation_errors(self, errors: list[str], answers: dict) -> bool:
        """Fill fields identified in Workday validation error messages.

        Parses human-readable error strings such as:
          "Given Name(s) - Latin Script is required"
          "Family Name - Latin Script is required"
          "Email is required"
          "Phone Number is required"

        Returns True if at least one field was successfully filled.
        """
        page = self._page
        job_id = self.job_hash[:8]
        filled_any = False

        for error in errors:
            el_lower = error.lower()

            # ── First / Given name ─────────────────────────────────────────────
            if "given name" in el_lower or "first name" in el_lower:
                val = answers.get("first_name", "")
                if val:
                    for sel in (
                        WD["first_name"],
                        'input[data-automation-id="legalNameSection_firstName"]',
                    ):
                        if _visible(page, sel):
                            try:
                                el = page.locator(sel).first
                                if not (el.input_value() or "").strip():
                                    el.fill(val)
                                    page.wait_for_timeout(150)
                                    filled_any = True
                                    logger.info(f"[{job_id}] Error-recovery: filled first_name")
                            except Exception:
                                pass
                    # Label fallback
                    if not filled_any:
                        for lbl in ("Given Name(s)", "Given Name(s) - Latin Script",
                                    "First Name", "Given Names - Latin Script"):
                            try:
                                loc = page.get_by_label(lbl, exact=False)
                                if loc.count() > 0 and loc.first.is_visible():
                                    if not (loc.first.input_value() or "").strip():
                                        loc.first.fill(val)
                                        page.wait_for_timeout(150)
                                        filled_any = True
                                        break
                            except Exception:
                                pass

            # ── Family / Last name ─────────────────────────────────────────────
            elif "family name" in el_lower or "last name" in el_lower:
                val = answers.get("last_name", "")
                if val:
                    for sel in (
                        WD["last_name"],
                        'input[data-automation-id="legalNameSection_lastName"]',
                    ):
                        if _visible(page, sel):
                            try:
                                el = page.locator(sel).first
                                if not (el.input_value() or "").strip():
                                    el.fill(val)
                                    page.wait_for_timeout(150)
                                    filled_any = True
                                    logger.info(f"[{job_id}] Error-recovery: filled last_name")
                            except Exception:
                                pass
                    if not filled_any:
                        for lbl in ("Family Name", "Family Name - Latin Script", "Last Name"):
                            try:
                                loc = page.get_by_label(lbl, exact=False)
                                if loc.count() > 0 and loc.first.is_visible():
                                    if not (loc.first.input_value() or "").strip():
                                        loc.first.fill(val)
                                        page.wait_for_timeout(150)
                                        filled_any = True
                                        break
                            except Exception:
                                pass

            # ── Email ──────────────────────────────────────────────────────────
            elif "email" in el_lower:
                val = answers.get("email", "")
                if val:
                    for sel in (
                        WD["email"],
                        'input[data-automation-id="emailAddress"]',
                        'input[data-automation-id="email-address"]',
                        'input[type="email"]',
                    ):
                        if _visible(page, sel):
                            try:
                                el = page.locator(sel).first
                                if not (el.input_value() or "").strip():
                                    el.fill(val)
                                    page.wait_for_timeout(150)
                                    filled_any = True
                                    logger.info(f"[{job_id}] Error-recovery: filled email")
                                    break
                            except Exception:
                                pass

            # ── Phone ──────────────────────────────────────────────────────────
            elif "phone" in el_lower:
                val = answers.get("phone", "")
                if val:
                    for sel in (
                        WD["phone"],
                        WD["phone_alt"],
                        'input[data-automation-id="phoneNumber"]',
                    ):
                        if _visible(page, sel):
                            try:
                                el = page.locator(sel).first
                                if not (el.input_value() or "").strip():
                                    el.fill(val)
                                    page.wait_for_timeout(150)
                                    filled_any = True
                                    logger.info(f"[{job_id}] Error-recovery: filled phone")
                                    break
                            except Exception:
                                pass
                    if not filled_any:
                        try:
                            loc = page.get_by_label("Phone Number", exact=False)
                            if loc.count() > 0 and loc.first.is_visible():
                                if not (loc.first.input_value() or "").strip():
                                    loc.first.fill(val)
                                    page.wait_for_timeout(150)
                                    filled_any = True
                                    logger.info(f"[{job_id}] Error-recovery: filled phone (label)")
                        except Exception:
                            pass

        return filled_any

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
