"""BaseAdapter — browser-level interface that all platform adapters implement.

Separation of concerns
----------------------
core/orchestrator.py  — state machine, retries, checkpointing    (zero LLM)
core/adapters/        — browser interaction                       (DOM first)
  base_adapter.py     — abstract interface + AdapterResult type
  generic_adapter.py  — generic implementation (DOM + Vision fallback)
  workday_adapter.py  — Workday-specific (Stage 4)
  amazon_adapter.py   — Amazon-specific   (Stage 4)

Contract
--------
Every adapter method MUST:
  1. Prefer deterministic DOM inspection over LLM/Vision
  2. Use Vision only as a fallback for field *identification*, not navigation
  3. Never let LLM output drive direct browser actions without deterministic execution
  4. Return AdapterResult — never raise (catch and return error result instead)
  5. Write a screenshot to AdapterResult.screenshot_path on every step

Page-state detection priority (analyze_entrypoint and detect_*)
---------------------------------------------------------------
  1. URL pattern matching (fastest, zero cost)
  2. DOM selector checks (fast, reliable)
  3. Visible text / aria-label heuristics
  4. Vision fallback ONLY if 1-3 are ambiguous
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Page


# ── AdapterResult ─────────────────────────────────────────────────────────────

@dataclass
class AdapterResult:
    """Structured result returned by every BaseAdapter method.

    The orchestrator reads ``next_state`` to drive transitions.
    All other fields are informational / persisted in the checkpoint.

    Attributes
    ----------
    next_state : str
        ApplyState value (string) — avoids circular import with orchestrator.
        Use the ``ApplyState`` enum values directly, e.g. ``"fill_form"``.
    success : bool
        Whether this step completed its goal without error.
    requires_verification : bool
        True when the site sent a verification email / OTP that must be resolved
        before the flow can continue.
    requires_manual_intervention : bool
        True for CAPTCHA, MFA app push, or any step a human must handle.
    error_message : str | None
        Human-readable failure reason (never contains secrets).
    screenshot_path : str | None
        Absolute path to the last screenshot taken in this step.
    metadata : dict
        Arbitrary adapter-specific data persisted in ApplyCheckpoint.metadata_json.
        Must be JSON-serialisable.  Never store secrets here.
    """
    next_state: str                               # ApplyState value
    success: bool = True
    requires_verification: bool = False
    requires_manual_intervention: bool = False
    error_message: str | None = None
    screenshot_path: str | None = None
    metadata: dict = field(default_factory=dict)

    # ── Convenience constructors ──────────────────────────────────────────────

    @classmethod
    def ok(cls, next_state: str, **kwargs) -> "AdapterResult":
        return cls(next_state=next_state, success=True, **kwargs)

    @classmethod
    def fail(cls, next_state: str, error: str, **kwargs) -> "AdapterResult":
        return cls(next_state=next_state, success=False, error_message=error, **kwargs)

    @classmethod
    def need_verification(cls, **kwargs) -> "AdapterResult":
        return cls(next_state="verify", success=True,
                   requires_verification=True, **kwargs)

    @classmethod
    def need_human(cls, reason: str, **kwargs) -> "AdapterResult":
        return cls(next_state="human_intervention", success=False,
                   requires_manual_intervention=True, error_message=reason, **kwargs)

    def to_metadata(self) -> dict:
        """Serialisable subset safe to persist in checkpoint.metadata_json."""
        return {
            "next_state": self.next_state,
            "success": self.success,
            "requires_verification": self.requires_verification,
            "requires_manual_intervention": self.requires_manual_intervention,
            "error_message": self.error_message,
            **(self.metadata or {}),
        }


# ── PageState — result of deterministic DOM analysis ─────────────────────────

@dataclass
class PageState:
    """What the adapter sees when it inspects the current page.

    Built entirely from DOM checks.  Vision is only consulted if
    ``is_ambiguous`` is True.

    Possible ``kind`` values
    ------------------------
    login        — email + password inputs visible (sign-in form)
    signup       — registration / create-account form
    two_fa       — OTP / verification-code input
    captcha      — CAPTCHA iframe or widget detected
    apply_button — job listing page with an Apply / Easy Apply button
    form         — application form fields already visible
    success      — confirmation / thank-you page
    error        — error page or error banner
    unknown      — none of the above; may need Vision
    """
    kind: str                           # one of the values above
    is_ambiguous: bool = False          # True → Vision fallback suggested
    apply_button_text: str | None = None
    details: dict = field(default_factory=dict)

    def is_auth(self) -> bool:
        return self.kind in ("login", "signup", "two_fa")

    def needs_intervention(self) -> bool:
        return self.kind in ("captcha",)

    def is_terminal(self) -> bool:
        return self.kind in ("success", "error")


# ── BaseAdapter ───────────────────────────────────────────────────────────────

class BaseAdapter(abc.ABC):
    """Abstract browser-level interface.

    Subclasses MUST implement every ``@abstractmethod``.
    All methods receive explicit ``page`` and ``context`` params — they never
    open their own browser; the parent ``AdapterBase`` in orchestrator.py
    manages the Playwright browser lifecycle.

    ``context`` is a plain dict of runtime data assembled by the orchestrator:
      context["job_hash"]        str  — short job ID for logging
      context["apply_url"]       str  — final application URL
      context["job_title"]       str
      context["company"]         str
      context["job_description"] str
      context["auto_submit"]     bool
      context["cv_path"]         str | None
      context["answers"]         dict — loaded from default_answers.yaml
      context["client"]          OpenAI — Groq client (for Vision fallback only)
    """

    # ── Class-level identity ──────────────────────────────────────────────────

    #: Canonical adapter name.  Used in logs, checkpoints, and the registry.
    name: str = "base"

    @classmethod
    def detect(cls, url: str) -> bool:
        """Return True if this adapter handles *url*.

        Called by the adapter registry to select the best adapter.
        URL-pattern matching only — no network I/O.
        """
        return False

    # ── Page state ────────────────────────────────────────────────────────────

    @abc.abstractmethod
    def analyze_entrypoint(self, page: "Page", context: dict) -> PageState:
        """Inspect the landing page and classify its current state.

        Implementation MUST:
          1. Use only DOM selectors and URL patterns (deterministic).
          2. Set ``is_ambiguous=True`` if the result is uncertain.
          3. Never make network calls or use LLM here.
        """

    def detect_manual_intervention(self, page: "Page", context: dict) -> bool:
        """Return True if the page requires human action (CAPTCHA, MFA push, etc.).

        Default implementation checks common CAPTCHA indicators via DOM.
        Subclasses may extend this with platform-specific checks.
        """
        return _dom_detect_captcha(page)

    # ── Authentication ────────────────────────────────────────────────────────

    @abc.abstractmethod
    def restore_session(self, page: "Page", session_data: str,
                        context: dict) -> bool:
        """Try to restore a saved Playwright storage_state.

        Returns True if the session is valid and the user is authenticated,
        False if the session expired and a fresh login is needed.
        """

    @abc.abstractmethod
    def login(self, page: "Page", email: str, password: str,
              context: dict) -> AdapterResult:
        """Fill and submit the login form.

        Result next_state values:
          fill_form           — login succeeded
          verify              — MFA / OTP required
          signup              — account does not exist
          failed              — unrecoverable error
        """

    @abc.abstractmethod
    def signup(self, page: "Page", email: str, password: str,
               context: dict) -> AdapterResult:
        """Create a new account on the platform.

        Result next_state values:
          verify              — email confirmation required
          fill_form           — account active immediately
          failed              — signup not supported or failed
        """

    # ── Application form ──────────────────────────────────────────────────────

    @abc.abstractmethod
    def fill_form(self, page: "Page", context: dict) -> AdapterResult:
        """Fill all visible application form fields.

        Implementation rules:
          1. Use deterministic CSS selectors for known field types.
          2. Use Vision ONLY for field *identification* (label → candidate key mapping).
          3. Handle multi-page forms by looping until no Next button is found.
          4. Never submit — return next_state="review" or "submit" to let
             the orchestrator decide.

        Result next_state values:
          review              — review page detected before submit
          submit              — ready to submit directly
          login               — session expired mid-form
          failed              — unrecoverable error
        """

    def review(self, page: "Page", context: dict) -> AdapterResult:
        """Handle a review / confirm-before-submit page if one is present.

        Default: no-op, returns next_state="submit".
        Subclasses override when the platform has a review step.
        """
        return AdapterResult.ok("submit")

    @abc.abstractmethod
    def submit(self, page: "Page", context: dict) -> AdapterResult:
        """Click the final submit button and confirm the application was sent.

        Result next_state values:
          success             — confirmation detected
          failed              — submission failed or confirmation not found
        """


# ── Shared DOM helpers (used by all adapters) ─────────────────────────────────

def dom_detect_page_state(page: "Page") -> PageState:
    """Run deterministic DOM checks and return a PageState.

    This is the standard implementation used by GenericAdapter and
    available to all subclasses.  No LLM, no network, no screenshots.
    """
    try:
        url = page.url.lower()

        # 1. URL-level quick wins
        for kw in ("thank", "success", "confirmation", "submitted", "complete"):
            if kw in url:
                return PageState(kind="success")

        # 2. Visible success / error text
        for sel, kind in (
            ('[class*="success"]', "success"),
            ('[class*="confirmation"]', "success"),
            ('[class*="thank"]', "success"),
            ('[class*="error"]:not(input):not(label)', "error"),
            ('[role="alert"]', "error"),
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible():
                    text = (el.inner_text() or "").lower()
                    if kind == "success" and any(
                        kw in text for kw in ("submitted", "received", "thank", "success", "complete")
                    ):
                        return PageState(kind="success")
            except Exception:
                pass

        # 3. CAPTCHA
        if _dom_detect_captcha(page):
            return PageState(kind="captcha")

        # 4. OTP / 2FA input (short single code field)
        otp_sels = [
            'input[autocomplete="one-time-code"]',
            'input[name*="otp"]', 'input[name*="code"]',
            'input[id*="otp"]', 'input[id*="code"]',
            'input[placeholder*="code" i]', 'input[placeholder*="otp" i]',
        ]
        for sel in otp_sels:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return PageState(kind="two_fa")
            except Exception:
                pass

        # 5. Login: visible email + password field pair
        has_email = _visible(page, 'input[type="email"], input[autocomplete="username"], '
                             'input[name*="email" i], input[id*="email" i]')
        has_password = _visible(page, 'input[type="password"]')
        if has_email and has_password:
            # Distinguish login from signup by looking for confirm-password field
            has_confirm = _visible(page,
                'input[autocomplete="new-password"], '
                'input[name*="confirm" i], input[id*="confirm" i], '
                'input[placeholder*="confirm" i]'
            )
            return PageState(kind="signup" if has_confirm else "login")

        # 6. Signup page without confirm field (e.g. Amazon first step)
        signup_indicators = [
            'button:has-text("Create account")', 'button:has-text("Sign up")',
            'a:has-text("Create account")', '[id*="createAccount"]',
            '[data-automation-id="createAccountLink"]',
        ]
        for sel in signup_indicators:
            try:
                if page.locator(sel).first.is_visible():
                    return PageState(kind="signup")
            except Exception:
                pass

        # 7. Application form already visible
        form_selectors = [
            'input:not([type="hidden"]):not([type="submit"]):not([type="button"])',
            "textarea", "select", 'input[type="file"]',
        ]
        for sel in form_selectors:
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 5)):
                    if loc.nth(i).is_visible():
                        return PageState(kind="form")
            except Exception:
                continue

        # 8. Apply button present (job listing page)
        apply_texts = [
            "Apply Now", "Apply for this job", "Apply for this position",
            "Apply to this job", "Apply", "Submit Application", "Start Application",
            "I'm interested", "Easy Apply",
        ]
        for text in apply_texts:
            for role in ("button", "link"):
                try:
                    loc = page.get_by_role(role, name=text, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        return PageState(kind="apply_button", apply_button_text=text)
                except Exception:
                    pass

        return PageState(kind="unknown", is_ambiguous=True)

    except Exception as exc:
        return PageState(kind="unknown", is_ambiguous=True, details={"exc": str(exc)})


def _visible(page: "Page", selector: str) -> bool:
    """Return True if at least one element matching *selector* is visible."""
    try:
        loc = page.locator(selector)
        for i in range(min(loc.count(), 3)):
            if loc.nth(i).is_visible():
                return True
    except Exception:
        pass
    return False


def _dom_detect_captcha(page: "Page") -> bool:
    """DOM-only CAPTCHA detection (no Vision)."""
    captcha_sels = [
        'iframe[src*="recaptcha"]',
        'iframe[src*="hcaptcha"]',
        'iframe[src*="challenges.cloudflare"]',
        '[data-sitekey]',
        '.g-recaptcha', '.h-captcha',
        '#cf-challenge-running',
        'div[class*="captcha"]',
    ]
    for sel in captcha_sels:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                return True
        except Exception:
            pass
    return False
