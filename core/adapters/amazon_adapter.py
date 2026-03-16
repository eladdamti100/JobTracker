"""AmazonAdapter — Amazon Jobs ATS adapter.

Amazon Flow
-----------
  navigate → detect page state (Amazon-specific DOM first)
  ├─ session valid  → restore_session → fill_form
  ├─ login page     → restore_session → login → [verify] → fill_form
  ├─ signup page    → signup → [verify] → fill_form
  └─ form page      → fill_form (multi-step) → review → submit

Key observations
----------------
Amazon Jobs uses its own authentication layer (separate from amazon.com consumer
accounts), hosted under hiring.amazon.com.  The auth flow is Amazon's standard
``ap/signin`` page, which is shared across all Amazon properties but scoped
specifically to the jobs portal.

Amazon-specific authentication selectors
-----------------------------------------
  input#ap_email                            — email field (standard Amazon auth)
  input#ap_password                         — password field
  input#signInSubmit                        — "Sign in" submit button
  input#auth-mfa-otpcode                    — OTP/MFA code input
  input#auth-signin-button                  — OTP submit button
  a#createAccountSubmit, a#ap-register-link — "Create account" links

CAPTCHA notes
--------------
Amazon uses Arkose Labs (FunCaptcha) for bot protection.  These CAPTCHA widgets
are invisible or embedded as iframes from ``client-api.arkoselabs.com`` or
``funcaptcha.com``.  Auto-solving is not feasible — always escalate to
HUMAN_INTERVENTION when detected.

Application form
----------------
Amazon's job application is hosted on hiring.amazon.com and is a multi-step
React form.  Pages vary by role category (tech, operations, corporate) and
include:
  - Personal information (name, phone pre-filled from account)
  - Resume / CV upload
  - Work authorization questions
  - Custom screening questions per role
  - (Optional) Knet online assessment — always triggers HUMAN_INTERVENTION

Session
-------
Playwright storage_state is encrypted and stored per domain
``hiring.amazon.com`` in the SessionStore table.  Amazon sessions typically
last 2–7 days.

Vision
------
Used ONLY if DOM detection returns is_ambiguous=True, and for unknown form
fields that can't be matched by label heuristics.  Never drives navigation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from openai import OpenAI

from core.adapters.base_adapter import (
    AdapterResult, PageState,
    dom_detect_page_state, _dom_detect_captcha, _visible,
)
from core.orchestrator import AdapterBase, ApplyState, StepResult, register_adapter

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page


# ── Constants ─────────────────────────────────────────────────────────────────

_PLATFORM_KEY     = "amazon"
_PAGE_LOAD_TIMEOUT = 60_000
_WAIT_AFTER_CLICK  = 1_500   # ms
_WAIT_AFTER_NAV    = 3_000   # ms — Amazon's React forms are slower than average
_MAX_FORM_PAGES    = 30

AMAZON_DOMAINS = (
    "amazon.jobs",
    "hiring.amazon.com",
    "www.amazon.jobs",
)

# Domain used for session save/restore (the apply-form host)
_SESSION_DOMAIN = "hiring.amazon.com"


# ── Amazon-specific selectors ─────────────────────────────────────────────────

AZ: dict[str, str] = {
    # ── Amazon auth ───────────────────────────────────────────────────────────
    # amazon.jobs uses its own form (not ap/signin).
    # Email field: plain input[type="email"] — no special id
    "email":         ('input[type="email"], '
                      'input[name*="email" i], '
                      'input#ap_email'),
    "password":      'input[type="password"], input#ap_password',
    # Log in button on amazon.jobs (orange button), fallback to generic submit
    "sign_in":       ('button:has-text("Log in"), '
                      'button:has-text("Sign in"), '
                      'button[type="submit"], '
                      'input#signInSubmit'),
    "otp":           ('input#auth-mfa-otpcode, input[name="otpCode"], '
                      'input[name="code"], input[autocomplete="one-time-code"]'),
    "otp_submit":    ('input#auth-signin-button, button:has-text("Sign in"), '
                      'button:has-text("Log in"), button[type="submit"]'),
    "create_acct":   ('a:has-text("Create an Amazon.jobs account"), '
                      'a:has-text("Create account"), '
                      '#createAccountSubmit, a#ap-register-link, '
                      'button:has-text("Create account")'),
    # Signup form fields
    "signup_name":   'input#ap_customer_name, input[name="customerName"]',
    "signup_email":  'input[type="email"], input#ap_email, input[name*="email" i]',
    "signup_pwd":    'input[type="password"], input#ap_password, input[name="ap_password"]',
    "signup_pwd2":   'input#ap_password_check, input[name="ap_password_check"]',
    "signup_submit": 'button:has-text("Create account"), input#continue, input[type="submit"]',

    # ── Application form ──────────────────────────────────────────────────────
    "first_name":    'input[id*="firstName" i], input[name*="firstName" i], '
                     'input[placeholder*="first name" i]',
    "last_name":     'input[id*="lastName" i], input[name*="lastName" i], '
                     'input[placeholder*="last name" i]',
    "phone":         'input[id*="phone" i], input[name*="phone" i], '
                     'input[type="tel"]',
    "resume":        'input[type="file"]',
    "cover_letter":  'textarea[id*="cover" i], textarea[name*="cover" i], '
                     'textarea[placeholder*="cover" i]',
    "linkedin":      'input[id*="linkedin" i], input[placeholder*="linkedin" i]',

    # ── Navigation ────────────────────────────────────────────────────────────
    "next_btn":      'button:has-text("Next"), button:has-text("Continue"), '
                     'a:has-text("Next"), button[type="submit"]',
    "submit_btn":    'button:has-text("Submit"), button:has-text("Apply"), '
                     'input[type="submit"]:not([id*="signIn"])',

    # ── Knet / assessment detection ───────────────────────────────────────────
    "assessment":    'iframe[src*="knet"], iframe[src*="assessment"], '
                     'div[class*="assessment"], h1:has-text("Assessment"), '
                     'h2:has-text("Online Test")',

    # ── State detection ───────────────────────────────────────────────────────
    "success_page":  'h1:has-text("Application"), h2:has-text("Applied"), '
                     'div[class*="confirmation"], div[class*="success"], '
                     '.application-complete',
    "arkose_captcha":'iframe[src*="arkoselabs"], iframe[src*="funcaptcha"], '
                     'iframe[src*="client-api.arkoselabs"]',
}


# ── AmazonAdapter ─────────────────────────────────────────────────────────────

class AmazonAdapter(AdapterBase):
    """Handles amazon.jobs and hiring.amazon.com job applications.

    Inherits AdapterBase (orchestrator.py) — state machine interface.
    Browser lifecycle is owned here: opened in plan(), closed in cleanup().
    """

    name = "amazon"

    @classmethod
    def detect(cls, url: str) -> bool:
        return any(d in url for d in AMAZON_DOMAINS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._playwright   = None
        self._browser: Browser | None       = None
        self._context: BrowserContext | None = None
        self._page: Page | None             = None
        self._client: OpenAI | None         = None
        self._screenshots: list[str]        = []
        self._cover_letter: str | None      = None

    # ── Orchestrator interface ────────────────────────────────────────────────

    def plan(self, checkpoint_meta: dict) -> StepResult:
        """Open browser, navigate to apply URL, detect initial page state."""
        try:
            self._open_browser()
            state = self._navigate_and_detect(self.apply_url, "01_plan")
            return self._page_state_to_step_result(state)
        except Exception as exc:
            logger.exception(f"[{self.job_hash[:8]}] AmazonAdapter.plan failed: {exc}")
            return StepResult(ApplyState.FAILED, success=False, error=str(exc))

    def restore_session(self, checkpoint_meta: dict) -> StepResult:
        """Try saved Amazon cookies; fall through to LOGIN if session is stale."""
        from core.credential_manager import load_session_state

        session_json = load_session_state(_SESSION_DOMAIN)
        if not session_json:
            logger.info(f"[{self.job_hash[:8]}] No saved Amazon session — need login")
            return StepResult(ApplyState.LOGIN)

        try:
            state_data = json.loads(session_json)
            self._context.add_cookies(state_data.get("cookies", []))
            self._page.reload(wait_until="networkidle", timeout=_PAGE_LOAD_TIMEOUT)
            self._page.wait_for_timeout(2_000)

            shot = self._safe_screenshot("az_session_restored")
            if shot:
                self._screenshots.append(shot)

            ps = self._az_dom_detect()
            if ps is None:
                ps = dom_detect_page_state(self._page)

            if ps.kind in ("login", "signup"):
                logger.info(f"[{self.job_hash[:8]}] Amazon session expired — need login")
                return StepResult(ApplyState.LOGIN, screenshot_path=shot)

            # If we're on the listing page (apply_button), navigate into the form first
            if ps.kind == "apply_button":
                logger.info(f"[{self.job_hash[:8]}] Session valid, on listing page — clicking Apply to enter form")
                clicked = self._click_apply_button()
                if clicked:
                    try:
                        self._page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        self._page.wait_for_timeout(_WAIT_AFTER_NAV)
                    shot = self._safe_screenshot("az_session_on_form")
                    if shot:
                        self._screenshots.append(shot)

            logger.info(f"[{self.job_hash[:8]}] Amazon session valid — proceeding to form")
            return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)

        except Exception as exc:
            logger.warning(f"[{self.job_hash[:8]}] Amazon session restore error: {exc}")
            return StepResult(ApplyState.LOGIN)

    def login(self, checkpoint_meta: dict) -> StepResult:
        """Fill Amazon login form with stored credentials."""
        from core.credential_manager import (
            get_credential, mark_login_success, mark_account_status,
        )

        before_shot = self._safe_screenshot("az_before_login")
        if before_shot:
            self._screenshots.append(before_shot)

        cred = get_credential(_PLATFORM_KEY)
        if not cred:
            logger.info(f"[{self.job_hash[:8]}] No Amazon credentials stored — will sign up")
            return StepResult(ApplyState.SIGNUP)

        email, password = cred

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
        if result.requires_manual_intervention:
            return StepResult(
                ApplyState.HUMAN_INTERVENTION, success=False,
                error=result.error_message,
                screenshot_path=result.screenshot_path,
            )
        if result.success:
            mark_login_success(_PLATFORM_KEY)
            # Save session so future runs skip login
            self._save_session()
            self._navigate_to_apply()
            return StepResult(
                ApplyState.FILL_FORM,
                screenshot_path=result.screenshot_path,
                meta=result.metadata,
            )

        logger.warning(f"[{self.job_hash[:8]}] Amazon login failed: {result.error_message}")
        return StepResult(
            ApplyState.SIGNUP,
            success=False,
            error=result.error_message,
            screenshot_path=result.screenshot_path,
        )

    def signup(self, checkpoint_meta: dict) -> StepResult:
        """Create a new Amazon Jobs account."""
        from core.credential_manager import (
            save_credential, generate_secure_password,
            PLATFORMS_NO_AUTO_SIGNUP,
        )
        from core.applicator import _get_answers

        if _PLATFORM_KEY in PLATFORMS_NO_AUTO_SIGNUP:
            from core.verifier import request_human_intervention
            request_human_intervention(
                self.job_hash, self.company, self.apply_url,
                "Amazon Jobs account required — please create one manually, then send DONE",
            )
            return StepResult(
                ApplyState.HUMAN_INTERVENTION, success=False,
                error="Auto-signup disabled for Amazon — manual account creation required",
            )

        answers = _get_answers()
        email = answers.get("email", os.environ.get("GMAIL_ADDRESS", ""))
        if not email:
            return StepResult(
                ApplyState.HUMAN_INTERVENTION, success=False,
                error="No email configured for Amazon signup (check default_answers.yaml)",
            )
        password = generate_secure_password(20)

        result = self._do_signup(email, password, answers)
        if result.screenshot_path:
            self._screenshots.append(result.screenshot_path)

        if result.success:
            save_credential(
                _PLATFORM_KEY, email, password,
                domain=_SESSION_DOMAIN,
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

        if result.requires_manual_intervention:
            return StepResult(
                ApplyState.HUMAN_INTERVENTION, success=False,
                error=result.error_message,
                screenshot_path=result.screenshot_path,
            )

        return StepResult(
            ApplyState.FAILED, success=False,
            error=result.error_message,
            screenshot_path=result.screenshot_path,
        )

    def verify(self, checkpoint_meta: dict) -> StepResult:
        """Handle Amazon OTP / CAPTCHA / email verification.

        Strategy (in order):
        1. Arkose CAPTCHA → HUMAN_INTERVENTION immediately (can't auto-solve)
        2. OTP input visible → ask user via WhatsApp, poll
        3. IMAP auto-verify (email link)
        4. No OTP input (email-link) → HUMAN_INTERVENTION
        """
        from core.verifier import (
            OTP_SELECTORS, request_otp_from_user, poll_for_otp,
            request_human_intervention, clear_verification_state,
            VERIFY_TIMEOUT_SECONDS,
        )

        page = self._page
        if not page:
            return StepResult(ApplyState.FAILED, success=False,
                              error="No browser page in verify()")

        shot = self._safe_screenshot("az_verify_page")
        if shot:
            self._screenshots.append(shot)

        # ── 1. Arkose / FunCaptcha → immediate HUMAN_INTERVENTION ─────────────
        if self._detect_arkose_captcha(page):
            logger.info(f"[{self.job_hash[:8]}] Amazon Arkose CAPTCHA detected")
            request_human_intervention(
                self.job_hash, self.company, self.apply_url,
                "Amazon CAPTCHA detected — please solve it in the browser and send DONE",
            )
            return StepResult(
                ApplyState.HUMAN_INTERVENTION, success=False,
                error="Amazon Arkose CAPTCHA requires manual action",
                screenshot_path=shot,
            )

        # ── 2. Amazon MFA/OTP input on page ───────────────────────────────────
        # Amazon's OTP field: input#auth-mfa-otpcode
        amazon_otp_sels = [AZ["otp"]] + OTP_SELECTORS
        otp_loc = None
        for sel in amazon_otp_sels:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible(timeout=1_000):
                    otp_loc = loc.first
                    break
            except Exception:
                continue

        if otp_loc is not None:
            request_otp_from_user(
                self.job_hash, self.company, "Amazon Jobs", self.apply_url
            )
            otp_code = poll_for_otp(self.job_hash, VERIFY_TIMEOUT_SECONDS)

            if not otp_code:
                clear_verification_state()
                shot2 = self._safe_screenshot("az_otp_timeout")
                return StepResult(
                    ApplyState.FAILED, success=False,
                    error="Amazon OTP timeout — no user response within 5 minutes",
                    screenshot_path=shot2 or shot,
                )
            try:
                otp_loc.fill(otp_code)
                page.wait_for_timeout(300)
                # Click OTP submit button
                otp_submit = page.locator(AZ["otp_submit"])
                if otp_submit.count() > 0 and otp_submit.first.is_visible():
                    otp_submit.first.click()
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception as exc:
                return StepResult(ApplyState.FAILED, success=False,
                                  error=f"Amazon OTP submit failed: {exc}")
            finally:
                del otp_code   # never hold OTP plaintext longer than needed

            self._navigate_to_apply()
            shot3 = self._safe_screenshot("az_after_otp")
            if shot3:
                self._screenshots.append(shot3)
            return StepResult(ApplyState.FILL_FORM, screenshot_path=shot3 or shot)

        # ── 3. IMAP auto-verify (email link) ──────────────────────────────────
        try:
            from core.applicator import _auto_verify_email, _screenshot as _ashot
            result_inner: dict = {}
            ok, _ = _auto_verify_email(
                _PLATFORM_KEY, page, self._client,
                self.job_hash[:8], 1, result_inner,
            )
            if ok:
                self._navigate_to_apply()
                auto_shot = str(_ashot(page, self.job_hash[:8], "az_verify_auto_done"))
                self._screenshots.append(auto_shot)
                logger.info(f"[{self.job_hash[:8]}] Amazon email verified via IMAP")
                return StepResult(ApplyState.FILL_FORM, screenshot_path=auto_shot)
        except Exception as exc:
            logger.debug(f"[{self.job_hash[:8]}] IMAP auto-verify not available: {exc}")

        # ── 4. Email-link only — user must click manually ──────────────────────
        request_human_intervention(
            self.job_hash, self.company, self.apply_url,
            "Amazon email verification — click the link in your email, then send DONE",
        )
        return StepResult(
            ApplyState.HUMAN_INTERVENTION, success=False,
            error="Amazon email verification required — waiting for user",
            screenshot_path=shot,
        )

    def fill_form(self, checkpoint_meta: dict) -> StepResult:
        """Multi-step Amazon application form."""
        try:
            result = self._do_fill_amazon_form()
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
            exc_str = str(exc)
            # TargetClosedError = browser/page closed after a click — likely submitted
            if "TargetClosed" in type(exc).__name__ or "has been closed" in exc_str:
                logger.info(f"[{self.job_hash[:8]}] Browser closed after form click — treating as submitted")
                return StepResult(ApplyState.SUBMIT, success=True,
                                  error=None, screenshot_path=None)
            logger.exception(f"[{self.job_hash[:8]}] AmazonAdapter.fill_form crashed: {exc}")
            shot = self._safe_screenshot("az_fill_crash")
            return StepResult(ApplyState.FAILED, success=False,
                              error=exc_str, screenshot_path=shot)

    def review(self, checkpoint_meta: dict) -> StepResult:
        """Amazon review step — screenshot and proceed to submit."""
        shot = self._safe_screenshot("az_review")
        if shot:
            self._screenshots.append(shot)
        logger.info(f"[{self.job_hash[:8]}] Amazon review page")
        return StepResult(ApplyState.SUBMIT, screenshot_path=shot)

    def submit(self, checkpoint_meta: dict) -> StepResult:
        """Click Amazon's final submit button and confirm success."""
        page = self._page
        job_id = self.job_hash[:8]

        try:
            if not self.auto_submit:
                shot = self._safe_screenshot("az_pre_submit_paused")
                return StepResult(
                    ApplyState.FAILED, success=False,
                    error="auto_submit=False — stopped before Amazon submit",
                    screenshot_path=shot,
                )

            submitted = False

            # If fill_form already redirected to search/jobs page, the form was submitted
            try:
                cur_url = page.url
                if ("amazon.jobs/en/search" in cur_url or "amazon.jobs/en/jobs" in cur_url
                        or checkpoint_meta.get("redirect_to_search")):
                    logger.info(f"[{job_id}] Already on search/jobs page — form was submitted during fill_form")
                    shot = self._safe_screenshot("az_after_submit")
                    if shot:
                        self._screenshots.append(shot)
                    return StepResult(
                        ApplyState.SUCCESS, screenshot_path=shot,
                        meta={"submitted": True, "confirmed": False,
                              "redirect_to_search": True, "screenshots": self._screenshots},
                    )
            except Exception:
                pass

            # Primary: Amazon-specific submit texts
            for text in ("Submit application", "Submit Application", "Submit", "Apply"):
                for role in ("button",):
                    try:
                        loc = page.get_by_role(role, name=text, exact=False)
                        if loc.count() > 0 and loc.first.is_visible():
                            loc.first.click()
                            page.wait_for_timeout(4_000)
                            submitted = True
                            logger.info(f"[{job_id}] Clicked Amazon submit: '{text}'")
                            break
                    except Exception:
                        continue
                if submitted:
                    break

            # Fallback: selector-based
            if not submitted:
                try:
                    loc = page.locator(AZ["submit_btn"])
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        page.wait_for_timeout(4_000)
                        submitted = True
                except Exception:
                    pass

            shot = self._safe_screenshot("az_after_submit")
            if shot:
                self._screenshots.append(shot)

            # Confirm success
            if self._detect_amazon_success(page):
                return StepResult(
                    ApplyState.SUCCESS, screenshot_path=shot,
                    meta={"screenshots": self._screenshots, "submitted": submitted},
                )

            # Vision fallback for confirmation
            if shot:
                state = self._vision_classify_state(shot)
                if state.kind == "success":
                    return StepResult(
                        ApplyState.SUCCESS, screenshot_path=shot,
                        meta={"screenshots": self._screenshots},
                    )

            if submitted:
                logger.warning(f"[{job_id}] Amazon submit clicked but confirmation not detected")
                return StepResult(
                    ApplyState.SUCCESS, screenshot_path=shot,
                    meta={"submitted": True, "confirmed": False,
                          "screenshots": self._screenshots},
                )

            return StepResult(
                ApplyState.FAILED, success=False,
                error="Amazon submit button not found",
                screenshot_path=shot,
            )

        except Exception as exc:
            shot = self._safe_screenshot("az_submit_crash")
            return StepResult(ApplyState.FAILED, success=False,
                              error=str(exc), screenshot_path=shot)

    def cleanup(self, final_state: ApplyState, error: str | None) -> None:
        """Encrypt and save Amazon session state, then close browser."""
        if self._page and self._browser:
            try:
                from core.credential_manager import save_session_state
                storage_state = self._context.storage_state()
                save_session_state(
                    domain=_SESSION_DOMAIN,
                    platform_key=_PLATFORM_KEY,
                    storage_state=json.dumps(storage_state),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=5),
                )
            except Exception as exc:
                logger.debug(f"Amazon session save skipped: {exc}")
            try:
                self._browser.close()
            except Exception:
                pass
            try:
                self._playwright.stop()
            except Exception:
                pass
        logger.info(
            f"[{self.job_hash[:8]}] AmazonAdapter cleanup "
            f"final={final_state.value} err={error!r}"
        )

    # ── Browser lifecycle ─────────────────────────────────────────────────────

    def _open_browser(self) -> None:
        """Open Playwright browser — idempotent."""
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
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        self._page = self._context.new_page()
        self._client = OpenAI(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            base_url=os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        )

    def _navigate_and_detect(self, url: str, label: str) -> PageState:
        """Navigate to *url* and detect page state (Amazon-specific first)."""
        try:
            self._page.goto(url, wait_until="networkidle", timeout=_PAGE_LOAD_TIMEOUT)
        except Exception:
            self._page.goto(url, wait_until="load", timeout=_PAGE_LOAD_TIMEOUT)
        self._page.wait_for_timeout(_WAIT_AFTER_NAV)
        self._dismiss_popups()

        shot = self._safe_screenshot(label)
        if shot:
            self._screenshots.append(shot)

        state = self._az_dom_detect()
        if state is None:
            state = dom_detect_page_state(self._page)
            if state.is_ambiguous and shot:
                state = self._vision_classify_state(shot)

        logger.info(
            f"[{self.job_hash[:8]}] Amazon page state: kind={state.kind} "
            f"url={self._page.url[:60]}"
        )
        return state

    def _navigate_to_apply(self) -> None:
        """Navigate back to apply URL after auth."""
        try:
            self._page.goto(self.apply_url, wait_until="networkidle",
                            timeout=_PAGE_LOAD_TIMEOUT)
            self._page.wait_for_timeout(_WAIT_AFTER_NAV)
            self._dismiss_popups()
        except Exception as exc:
            logger.debug(f"[{self.job_hash[:8]}] _navigate_to_apply: {exc}")

    # ── Amazon-specific DOM detection ─────────────────────────────────────────

    def _az_dom_detect(self) -> PageState | None:
        """Check Amazon-specific selectors before falling back to generic detection.

        Returns a PageState when Amazon-specific state is detected, else None.
        """
        page = self._page

        # Arkose / FunCaptcha
        if self._detect_arkose_captcha(page):
            return PageState(kind="captcha")

        url = page.url.lower()

        # ── Amazon job LISTING page (amazon.jobs/en/jobs/…) ──────────────────
        # Must be checked BEFORE generic form detection because the listing page
        # has a search bar <input> that dom_detect_page_state() mistakes for a form.
        if "amazon.jobs/en/jobs/" in url and "/apply" not in url:
            # Look for the Apply Now button/link on the listing page
            for sel in (
                'a[href*="/apply"]',
                'a.apply-button',
                'button.apply-button',
                'a:has-text("Apply now")',
                'a:has-text("Apply for this job")',
            ):
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        text = loc.first.inner_text().strip() or "Apply now"
                        return PageState(kind="apply_button", apply_button_text=text)
                except Exception:
                    continue
            # Listing page loaded but no Apply button visible yet — still "apply_button"
            return PageState(kind="apply_button", apply_button_text="Apply now")

        # OTP on any page
        if _visible(page, AZ["otp"]):
            return PageState(kind="two_fa")

        # Assessment / Knet page — always requires human
        if self._detect_assessment(page):
            return PageState(kind="captcha",
                             details={"reason": "assessment_required"})

        # Auth page detection — covers both ap/signin AND amazon.jobs own login form
        # amazon.jobs login: email + password on same page, "Log in" button
        # ap/signin: same email+password structure
        if _visible(page, AZ["email"]) and _visible(page, AZ["password"]):
            if _visible(page, AZ["signup_pwd2"]):
                return PageState(kind="signup")
            return PageState(kind="login")

        # Signup-link only (no form yet) — e.g. amazon.jobs "Create an Amazon.jobs account"
        if _visible(page, AZ["create_acct"]):
            return PageState(kind="signup")

        # Success indicators
        if self._detect_amazon_success(page):
            return PageState(kind="success")

        return None  # fall through to generic detection

    def _page_state_to_step_result(self, state: PageState) -> StepResult:
        """Map PageState to the correct next ApplyState."""
        shot = self._screenshots[-1] if self._screenshots else None
        kind = state.kind

        if kind == "success":
            return StepResult(ApplyState.SUCCESS, screenshot_path=shot)

        if kind == "captcha":
            reason = state.details.get("reason", "")
            if reason == "assessment_required":
                from core.verifier import request_human_intervention
                request_human_intervention(
                    self.job_hash, self.company, self.apply_url,
                    "Amazon online assessment detected — please complete it and send DONE",
                )
                return StepResult(
                    ApplyState.HUMAN_INTERVENTION, success=False,
                    error="Amazon assessment requires manual completion",
                    screenshot_path=shot,
                )
            return StepResult(
                ApplyState.HUMAN_INTERVENTION, success=False,
                error="Amazon CAPTCHA detected on landing page",
                screenshot_path=shot,
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
            clicked = self._click_apply_button(state.apply_button_text)
            if clicked:
                # Wait for navigation to settle — Amazon may do multiple redirects
                # (amazon.jobs → /apply → hiring.amazon.com or ap/signin)
                try:
                    self._page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    self._page.wait_for_timeout(_WAIT_AFTER_NAV)
                self._dismiss_popups()
                shot2 = self._safe_screenshot("az_after_apply_click")
                if shot2:
                    self._screenshots.append(shot2)
                state2 = self._az_dom_detect() or dom_detect_page_state(self._page)
                return self._page_state_to_step_result(state2)
            return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)

        # unknown — Vision one more time, then fall through
        if shot:
            state2 = self._vision_classify_state(shot)
            if not state2.is_ambiguous:
                return self._page_state_to_step_result(state2)
        return StepResult(ApplyState.FILL_FORM, screenshot_path=shot)

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _do_login(self, email: str, password: str) -> AdapterResult:
        """Fill Amazon login form (ap/signin pattern)."""
        page = self._page
        job_id = self.job_hash[:8]

        try:
            # Amazon's two-step login: email first, then password on next page
            # Step 1: wait for auth page to load, then fill email
            try:
                self._page.wait_for_selector(AZ["email"], timeout=10_000)
            except Exception:
                pass  # proceed anyway — _fill_input will handle the None case

            if not self._fill_input(AZ["email"], email, "email"):
                return AdapterResult.fail(
                    "signup", "Could not find Amazon email input"
                )
            page.wait_for_timeout(400)

            # amazon.jobs login is single-step: email + password on same page.
            # There is no "Continue" click between them.
            if not self._fill_input(AZ["password"], password, "password"):
                return AdapterResult.fail(
                    "failed", "Could not find Amazon password input"
                )
            page.wait_for_timeout(300)

            # Click "Log in" / "Sign in" button
            signed_in = False
            sign_in_loc = page.locator(AZ["sign_in"])
            if sign_in_loc.count() > 0 and sign_in_loc.first.is_visible():
                sign_in_loc.first.click()
                signed_in = True

            page.wait_for_timeout(4_000)
            shot = self._safe_screenshot("az_after_login")
            if shot:
                self._screenshots.append(shot)

            # Arkose CAPTCHA after login attempt
            if self._detect_arkose_captcha(page):
                return AdapterResult.need_human(
                    "Amazon Arkose CAPTCHA after login — manual action required",
                    screenshot_path=shot,
                )

            # Check body text for error indicators
            try:
                body = (page.locator("body").inner_text() or "").lower()
                invalid_kws = (
                    "your password is incorrect", "cannot find an account",
                    "no account found", "incorrect password", "password is wrong",
                    "we cannot find an account", "email address or mobile number",
                )
                if any(kw in body for kw in invalid_kws):
                    logger.warning(f"[{job_id}] Amazon login credentials rejected")
                    return AdapterResult.fail(
                        "signup",
                        "Amazon login credentials invalid — will try signup",
                        screenshot_path=shot,
                    )
                if any(kw in body for kw in (
                    "verify your email", "check your email", "verification email",
                    "enter the otp", "enter the code",
                )):
                    return AdapterResult.need_verification(
                        screenshot_path=shot,
                        metadata={"reason": "email_verification_required"},
                    )
            except Exception:
                pass

            # OTP / MFA field appeared
            if _visible(page, AZ["otp"]):
                return AdapterResult.need_verification(
                    screenshot_path=shot,
                    metadata={"reason": "mfa_otp"},
                )

            # Still on auth page → credentials rejected
            url = page.url.lower()
            if "ap/signin" in url or "ap/register" in url:
                return AdapterResult.fail(
                    "signup",
                    "Still on Amazon auth page after login — credentials may be invalid",
                    screenshot_path=shot,
                )

            return AdapterResult.ok("fill_form", screenshot_path=shot)

        except Exception as exc:
            shot = self._safe_screenshot("az_login_error")
            return AdapterResult.fail("failed", str(exc), screenshot_path=shot)

    def _do_signup(self, email: str, password: str, answers: dict) -> AdapterResult:
        """Create a new Amazon Jobs account."""
        page = self._page
        job_id = self.job_hash[:8]

        try:
            # Navigate to apply URL — Amazon will redirect to its auth page
            try:
                page.goto(self.apply_url, wait_until="networkidle",
                          timeout=_PAGE_LOAD_TIMEOUT)
                page.wait_for_timeout(2_000)
            except Exception:
                pass

            # Click "Create account" link
            create_loc = page.locator(AZ["create_acct"])
            if create_loc.count() > 0 and create_loc.first.is_visible():
                create_loc.first.click()
                page.wait_for_timeout(2_000)
            else:
                # Try text-based search
                for text in (
                    "Create account", "Create an Amazon.jobs account",
                    "Register", "New user", "Sign up",
                ):
                    for role in ("link", "button"):
                        try:
                            loc = page.get_by_role(role, name=text, exact=False)
                            if loc.count() > 0 and loc.first.is_visible():
                                loc.first.click()
                                page.wait_for_timeout(2_000)
                                break
                        except Exception:
                            continue

            shot = self._safe_screenshot("az_signup_page")
            if shot:
                self._screenshots.append(shot)

            # Arkose CAPTCHA on signup page — can't auto-create account
            if self._detect_arkose_captcha(page):
                return AdapterResult.need_human(
                    "Amazon CAPTCHA on signup page — please create account manually and send DONE",
                    screenshot_path=shot,
                )

            # Fill name
            full_name = (
                f"{answers.get('first_name', '')} {answers.get('last_name', '')}".strip()
            )
            self._fill_input(AZ["signup_name"], full_name, "signup_name")
            page.wait_for_timeout(200)

            # Fill email
            self._fill_input(AZ["signup_email"], email, "signup_email")
            page.wait_for_timeout(200)

            # Fill password (and confirm if present)
            self._fill_input(AZ["signup_pwd"], password, "signup_password")
            page.wait_for_timeout(200)
            if _visible(page, AZ["signup_pwd2"]):
                self._fill_input(AZ["signup_pwd2"], password, "signup_password_confirm")
                page.wait_for_timeout(200)

            page.wait_for_timeout(300)

            # Submit signup
            submit_loc = page.locator(AZ["signup_submit"])
            if submit_loc.count() > 0 and submit_loc.first.is_visible():
                submit_loc.first.click()
            else:
                page.locator('button[type="submit"], input[type="submit"]').first.click()

            page.wait_for_timeout(4_000)
            shot = self._safe_screenshot("az_after_signup")
            if shot:
                self._screenshots.append(shot)

            # Post-signup state detection
            if self._detect_arkose_captcha(page):
                return AdapterResult.need_human(
                    "Amazon CAPTCHA after signup — please solve it and send DONE",
                    screenshot_path=shot,
                )

            try:
                body = (page.locator("body").inner_text() or "").lower()
                if any(kw in body for kw in (
                    "verify your email", "check your email", "confirmation email",
                    "we've sent", "we sent", "verification link", "click the link",
                )):
                    logger.info(f"[{job_id}] Amazon requires email verification after signup")
                    return AdapterResult.need_verification(screenshot_path=shot)

                if any(kw in body for kw in (
                    "already exists", "account with that email", "account exists",
                )):
                    # Account already exists — try login instead
                    logger.info(f"[{job_id}] Amazon account already exists — trying login")
                    return AdapterResult.fail(
                        "login",
                        "Amazon account already exists — switching to login flow",
                        screenshot_path=shot,
                    )
            except Exception:
                pass

            # OTP appeared
            if _visible(page, AZ["otp"]):
                return AdapterResult.need_verification(
                    screenshot_path=shot,
                    metadata={"reason": "otp_after_signup"},
                )

            url = page.url.lower()
            if "ap/signin" in url or "ap/register" in url:
                return AdapterResult.fail(
                    "failed",
                    "Amazon signup failed — still on auth page",
                    screenshot_path=shot,
                )

            return AdapterResult.ok("fill_form", screenshot_path=shot)

        except Exception as exc:
            shot = self._safe_screenshot("az_signup_error")
            return AdapterResult.fail("failed", str(exc), screenshot_path=shot)

    # ── Application form ──────────────────────────────────────────────────────

    def _do_fill_amazon_form(self) -> AdapterResult:
        """Multi-step Amazon application form.

        Amazon's application is a React SPA with multiple steps:
          1. Personal info — name, phone (pre-filled from account)
          2. Work authorization — questions about eligibility
          3. Resume upload
          4. Screening questions — role-specific
          5. (Optional) Review → Submit

        Per page:
          1. Check assessment / CAPTCHA / success (terminal states)
          2. Fill known fields (deterministic selectors)
          3. Vision for remaining unknown fields
          4. Consent checkboxes
          5. Click Next / Submit
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
        last_url: str = ""
        stuck_count: int = 0

        for page_num in range(1, _MAX_FORM_PAGES + 1):
            logger.info(f"[{job_id}] Amazon form page {page_num}")
            page.wait_for_timeout(1_500)

            # ── Stuck-page detection ──────────────────────────────────────────
            current_url = page.url
            if current_url == last_url:
                stuck_count += 1
                if stuck_count >= 3:
                    logger.warning(f"[{job_id}] Stuck on same page for {stuck_count} iterations: {current_url}")
                    # Ask user to handle the stuck page
                    from core.verifier import request_human_intervention
                    request_human_intervention(
                        self.job_hash, self.company, self.apply_url,
                        "Form stuck on a page — there may be a required field or CAPTCHA. "
                        "Please complete the form in the browser and send DONE",
                    )
                    return AdapterResult.need_human(
                        f"Form stuck on page {page_num} — possible required field or CAPTCHA",
                        screenshot_path=last_shot,
                    )
            else:
                stuck_count = 0
            last_url = current_url

            # ── Terminal / special state checks ───────────────────────────────
            if self._detect_amazon_success(page):
                return AdapterResult.ok("success", screenshot_path=last_shot)

            if self._detect_assessment(page):
                from core.verifier import request_human_intervention
                request_human_intervention(
                    self.job_hash, self.company, self.apply_url,
                    "Amazon online assessment — please complete it in the browser, then send DONE",
                )
                return AdapterResult.need_human(
                    "Amazon assessment requires manual completion",
                    screenshot_path=last_shot,
                )

            if self._detect_arkose_captcha(page):
                from core.verifier import request_human_intervention
                request_human_intervention(
                    self.job_hash, self.company, self.apply_url,
                    "Amazon CAPTCHA during form filling — please solve it and send DONE",
                )
                return AdapterResult.need_human("CAPTCHA during Amazon form", screenshot_path=last_shot)

            # ── Screenshot for Vision ─────────────────────────────────────────
            shot_path = _app_screenshot(page, job_id, f"az_form_{page_num:02d}")
            last_shot = str(shot_path)
            self._screenshots.append(last_shot)

            # ── Step 1: Deterministic Amazon field fills ───────────────────────
            self._fill_known_amazon_fields(answers, cv_path)

            # ── Step 2: Vision for remaining / unknown fields ──────────────────
            known_keys = {
                "first_name", "last_name", "phone", "email",
                "resume", "cover_letter", "linkedin",
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
                    if candidate_key in known_keys:
                        continue
                    value = lookup_answer(candidate_key, answers, label, self._client)
                    if value:
                        _fill_field(page, field, value, answers, cv_path, 1)
                except Exception as exc:
                    logger.debug(f"[{job_id}] Vision fill ({field.get('label')}): {exc}")

            # ── Step 3: Consent checkboxes ─────────────────────────────────────
            _check_consent_checkboxes(page, 1)
            page.wait_for_timeout(500)

            # ── Step 4: Navigate to next page ──────────────────────────────────
            nav = self._click_amazon_nav(page, field_data if fields else {}, job_id)

            if nav == "submit_clicked":
                page.wait_for_timeout(4_000)
                shot_after = self._safe_screenshot("az_after_submit_nav")
                if shot_after:
                    self._screenshots.append(shot_after)
                return AdapterResult.ok("submit", screenshot_path=shot_after,
                                        metadata={"pages_filled": page_num})

            if nav == "next_clicked":
                try:
                    page.wait_for_timeout(2_500)
                except Exception as nav_exc:
                    # Browser may have closed or page navigated away — treat as success
                    logger.info(f"[{job_id}] Page closed after next click (likely submitted): {nav_exc}")
                    return AdapterResult.ok("submit", screenshot_path=last_shot,
                                           metadata={"pages_filled": page_num})
                # Check if we navigated away from the hiring form (possible success)
                try:
                    cur_url = page.url
                    if "amazon.jobs/en/search" in cur_url or "amazon.jobs/en/jobs" in cur_url:
                        if self._detect_amazon_success(page):
                            return AdapterResult.ok("success", screenshot_path=last_shot)
                        # Navigated to search — may have submitted
                        logger.info(f"[{job_id}] Navigated to search after Next — possible success")
                        return AdapterResult.ok("submit", screenshot_path=last_shot,
                                               metadata={"pages_filled": page_num, "redirect_to_search": True})
                except Exception:
                    pass
                continue

            if nav == "no_button":
                state = dom_detect_page_state(page)
                if state.kind == "success":
                    return AdapterResult.ok("success", screenshot_path=last_shot)
                return AdapterResult.ok("submit", screenshot_path=last_shot,
                                        metadata={"pages_filled": page_num})

        shot = self._safe_screenshot("az_form_limit")
        return AdapterResult.fail(
            "failed",
            f"Reached Amazon form page limit ({_MAX_FORM_PAGES}) without submitting",
            screenshot_path=shot,
        )

    def _fill_known_amazon_fields(self, answers: dict, cv_path) -> None:
        """Fill Amazon fields identifiable by stable selectors.

        Only fills fields that are currently visible and empty.
        """
        page = self._page

        direct_map = {
            AZ["first_name"]:   answers.get("first_name", ""),
            AZ["last_name"]:    answers.get("last_name", ""),
            AZ["phone"]:        answers.get("phone", ""),
            AZ["linkedin"]:     answers.get("linkedin_url", ""),
            AZ["cover_letter"]: answers.get("cover_letter", answers.get("about_me", "")),
        }
        for selector, value in direct_map.items():
            if not value or not _visible(page, selector):
                continue
            try:
                el = page.locator(selector).first
                current = ""
                try:
                    current = (el.input_value() or "").strip()
                except Exception:
                    try:
                        current = (el.inner_text() or "").strip()
                    except Exception:
                        pass
                if not current:
                    el.fill(value)
                    page.wait_for_timeout(150)
            except Exception as exc:
                logger.debug(f"Amazon field ({selector[:50]}): {exc}")

        # Resume upload — only if a file input is present
        if cv_path and _visible(page, AZ["resume"]):
            try:
                fu = page.locator(AZ["resume"]).first
                fu.set_input_files(str(cv_path))
                page.wait_for_timeout(2_000)
                logger.info(f"[{self.job_hash[:8]}] CV uploaded to Amazon form")
            except Exception as exc:
                logger.debug(f"Amazon CV upload: {exc}")

    def _click_amazon_nav(self, page: "Page", field_data: dict, job_id: str) -> str:
        """Click the active Amazon navigation button.

        Returns: 'submit_clicked' | 'next_clicked' | 'no_button'
        """
        from core.applicator import NEXT_BUTTON_TEXTS, SUBMIT_BUTTON_TEXTS

        # Build candidate list — Vision hints first, then known texts
        vision_next_text = field_data.get("next_button_text", "")
        vision_submit_text = field_data.get("submit_button_text", "")
        candidates = []
        if field_data.get("submit_button") and vision_submit_text:
            candidates.append(("submit", vision_submit_text))
        if field_data.get("next_button") and vision_next_text:
            candidates.append(("next", vision_next_text))
        for t in SUBMIT_BUTTON_TEXTS:
            candidates.append(("submit", t))
        for t in NEXT_BUTTON_TEXTS:
            candidates.append(("next", t))

        seen: set[tuple[str, str]] = set()
        for nav_type, text in candidates:
            key = (nav_type, text.lower())
            if key in seen:
                continue
            seen.add(key)
            for role in ("button",):
                try:
                    loc = page.get_by_role(role, name=text, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        page.wait_for_timeout(800)
                        return "submit_clicked" if nav_type == "submit" else "next_clicked"
                except Exception:
                    continue

        # Fallback: generic Next / submit selector
        for sel, nav_type in ((AZ["next_btn"], "next"), (AZ["submit_btn"], "submit")):
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    page.wait_for_timeout(800)
                    return "submit_clicked" if nav_type == "submit" else "next_clicked"
            except Exception:
                continue

        return "no_button"

    # ── Shared DOM helpers ────────────────────────────────────────────────────

    def _detect_arkose_captcha(self, page: "Page") -> bool:
        """Detect Arkose Labs / FunCaptcha challenge (Amazon-specific CAPTCHA)."""
        for sel in (
            AZ["arkose_captcha"],
            '[id*="arkoselabs"]', '[class*="arkoselabs"]',
            '[id*="funcaptcha"]', '[class*="funcaptcha"]',
            '#captchacharacters',         # Amazon's text-based CAPTCHA fallback
            'img[src*="captcha"]',
        ):
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _detect_assessment(self, page: "Page") -> bool:
        """Detect Amazon's online assessment (Knet / HireVue) redirect."""
        try:
            url = page.url.lower()
            if any(kw in url for kw in ("knet", "assessment", "hirevue", "pymetrics")):
                return True
            try:
                if page.locator(AZ["assessment"]).count() > 0:
                    return True
            except Exception:
                pass
            try:
                body = (page.locator("h1, h2").first.inner_text() or "").lower()
                if any(kw in body for kw in ("assessment", "online test", "work sample")):
                    return True
            except Exception:
                pass
        except Exception:
            pass
        return False

    def _detect_amazon_success(self, page: "Page") -> bool:
        """DOM-only Amazon confirmation detection."""
        try:
            url = page.url.lower()
            if any(kw in url for kw in ("confirmation", "success", "submitted", "thank")):
                return True
            try:
                if page.locator(AZ["success_page"]).count() > 0:
                    el = page.locator(AZ["success_page"]).first
                    text = (el.inner_text() or "").lower()
                    if any(kw in text for kw in (
                        "thank", "submitted", "received", "complete", "applied",
                    )):
                        return True
            except Exception:
                pass
            try:
                body = (page.locator("body").inner_text() or "").lower()
                if any(kw in body for kw in (
                    "application submitted", "thank you for applying",
                    "your application has been submitted",
                    "we've received your application",
                    "application complete", "successfully applied",
                )):
                    return True
            except Exception:
                pass
        except Exception:
            pass
        return False

    def _fill_input(self, selector: str, value: str, field_name: str) -> bool:
        """Fill first visible input matching *selector*."""
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
        """Close cookie banners / overlays that block the page."""
        for sel in (
            'button:has-text("Accept")', 'button:has-text("Accept All")',
            'button:has-text("Got it")', 'button:has-text("Dismiss")',
            '[aria-label="Close"]', '[aria-label="close"]',
            '#sp-cc-accept',        # Amazon's cookie accept
            '#a-autoid-0',          # Amazon cookie banner ID
        ):
            try:
                loc = self._page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    self._page.wait_for_timeout(400)
                    return
            except Exception:
                continue

    def _click_apply_button(self, known_text: str | None = None) -> bool:
        """Click the Apply button on a job listing page.

        amazon.jobs uses <a href="…/apply"> links, not <button> elements,
        so we try CSS selector first, then ARIA role fallback.
        """
        page = self._page

        # 1. CSS selector — fastest, works for amazon.jobs <a href="…/apply">
        for sel in (
            'a[href*="/apply"]',
            'a.apply-button',
            'button.apply-button',
        ):
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.debug(f"[{self.job_hash[:8]}] Clicked apply via selector: {sel}")
                    return True
            except Exception:
                continue

        # 2. Text / role fallback
        texts = ([known_text] if known_text else []) + [
            "Apply now", "Apply Now", "Apply for this job", "Apply", "Start application",
        ]
        for text in texts:
            for role in ("link", "button"):
                try:
                    loc = page.get_by_role(role, name=text, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        logger.debug(f"[{self.job_hash[:8]}] Clicked apply via role+text: {text}")
                        return True
                except Exception:
                    continue
        return False

    def _safe_screenshot(self, name: str) -> str | None:
        """Take a screenshot without raising."""
        if not self._page:
            return None
        try:
            from config.settings import SCREENSHOTS_DIR
            shot_dir = SCREENSHOTS_DIR / self.job_hash[:8]
            shot_dir.mkdir(parents=True, exist_ok=True)
            path = shot_dir / f"{name}.png"
            try:
                self._page.screenshot(path=str(path), full_page=True, timeout=8_000)
            except Exception:
                self._page.screenshot(path=str(path), full_page=False, timeout=5_000)
            return str(path)
        except Exception as exc:
            logger.debug(f"Screenshot failed ({name}): {exc}")
            return None

    def _vision_classify_state(self, screenshot_path: str) -> PageState:
        """Use Groq Vision to classify an ambiguous page state."""
        try:
            import base64
            with open(screenshot_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()

            resp = self._client.chat.completions.create(
                model=os.environ.get("GROQ_VISION_MODEL",
                                     "meta-llama/llama-4-scout-17b-16e-instruct"),
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Classify this webpage. Reply with EXACTLY ONE word:\n"
                                "login, signup, two_fa, captcha, form, apply_button, success, error, unknown"
                            ),
                        },
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                    ],
                }],
                max_tokens=10,
                temperature=0,
            )
            kind = (resp.choices[0].message.content or "unknown").strip().lower()
            valid = {"login", "signup", "two_fa", "captcha", "form",
                     "apply_button", "success", "error", "unknown"}
            return PageState(kind=kind if kind in valid else "unknown",
                             is_ambiguous=(kind not in valid))
        except Exception as exc:
            logger.debug(f"Vision classify failed: {exc}")
            return PageState(kind="unknown", is_ambiguous=True)

    def _save_session(self) -> None:
        """Persist current browser cookies so future runs skip login."""
        try:
            from core.credential_manager import save_session_state
            cookies = self._context.cookies()
            save_session_state(_SESSION_DOMAIN, json.dumps({"cookies": cookies}))
            logger.info(f"[{self.job_hash[:8]}] Amazon session saved ({len(cookies)} cookies)")
        except Exception as exc:
            logger.debug(f"Session save failed: {exc}")

    def _resolve_cv(self):
        """Return the CV path from config."""
        try:
            from config.settings import DATA_DIR
            cv = DATA_DIR / "CV Resume.pdf"
            return cv if cv.exists() else None
        except Exception:
            return None


# ── Self-registration ──────────────────────────────────────────────────────────
register_adapter("amazon", AmazonAdapter)
