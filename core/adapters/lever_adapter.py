"""LeverAdapter — Lever ATS platform adapter.

Lever Flow
----------
  navigate → detect page state
  └─ form page → fill_form (single-page, deterministic) → submit → success

Key observations
----------------
- Forms on jobs.lever.co are always publicly accessible (no login required).
- Lever uses stable ``name`` attributes on all core inputs:
    input[name="name"]       — full name
    input[name="email"]
    input[name="phone"]
    input[name="resume"]     — file upload (hidden, set via set_input_files)
    textarea[name="comments"] — cover letter / additional info
- Social links follow a pattern:
    input[name="urls[LinkedIn]"]
    input[name="urls[GitHub]"]
    input[name="urls[Portfolio]"]
    input[name="urls[Twitter]"]
    input[name="urls[Other]"]
- Custom questions are contained in <div class="application-question"> blocks,
  each with a label and an input / textarea / select / checkbox group.
- Submit button: button[type="submit"] with text "Submit application" (or similar).
- Confirmation: div.thanks or h1 containing "Thanks" / "Application submitted".

Vision
------
Used only as fallback for custom questions whose labels can't be matched
deterministically.  Never drives navigation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from core.adapters.base_adapter import AdapterResult, _dom_detect_captcha, _visible
from core.adapters.generic_adapter import GenericAdapter
from core.orchestrator import ApplyState, StepResult, register_adapter

if TYPE_CHECKING:
    from playwright.sync_api import Page


# ── Constants ─────────────────────────────────────────────────────────────────

_PLATFORM_KEY = "lever"

LEVER_DOMAINS = (
    "jobs.lever.co",
    "lever.co",
)

# Stable Lever field selectors
LV: dict[str, str] = {
    "name":         'input[name="name"]',
    "email":        'input[name="email"]',
    "phone":        'input[name="phone"]',
    "resume":       'input[name="resume"], input[type="file"][id*="resume" i]',
    "cover_letter": 'textarea[name="comments"], textarea[name="coverLetter"], '
                    'textarea[placeholder*="cover" i], textarea[placeholder*="additional" i]',
    "linkedin":     'input[name="urls[LinkedIn]"], input[placeholder*="linkedin" i]',
    "github":       'input[name="urls[GitHub]"], input[placeholder*="github" i]',
    "portfolio":    'input[name="urls[Portfolio]"], input[placeholder*="portfolio" i]',
    "twitter":      'input[name="urls[Twitter]"]',
    "other_url":    'input[name="urls[Other]"]',
    "submit":       'button[type="submit"], input[type="submit"]',
}

# Custom question container used in Lever forms
_QUESTION_SEL = "div.application-question, div[class*='question']"


# ── LeverAdapter ──────────────────────────────────────────────────────────────

class LeverAdapter(GenericAdapter):
    """Handles jobs.lever.co application forms.

    Inherits browser infrastructure from GenericAdapter.
    Overrides fill_form() with Lever-specific deterministic selectors.
    """

    name = "lever"

    @classmethod
    def detect(cls, url: str) -> bool:
        return any(d in url for d in LEVER_DOMAINS)

    # ── Orchestrator interface ────────────────────────────────────────────────

    def plan(self, checkpoint_meta: dict) -> StepResult:
        """Open browser, navigate to apply_url, detect page state.

        Lever job pages are publicly accessible — expect 'form' or 'apply_button'.
        Inherits GenericAdapter.plan() unchanged.
        """
        return super().plan(checkpoint_meta)

    def fill_form(self, checkpoint_meta: dict) -> StepResult:
        """Fill Lever application form with deterministic selectors."""
        try:
            result = self._do_fill_lever_form()
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
            logger.exception(f"[{self.job_hash[:8]}] LeverAdapter.fill_form failed: {exc}")
            shot = self._safe_screenshot("lv_fill_crash")
            return StepResult(ApplyState.FAILED, success=False,
                              error=str(exc), screenshot_path=shot)

    # ── Lever-specific form filling ───────────────────────────────────────────

    def _do_fill_lever_form(self) -> AdapterResult:
        """Deterministic Lever form fill.

        Steps:
        1. Click "Apply" button if on a job listing page.
        2. Fill core fields (name, email, phone) via stable name attributes.
        3. Upload resume.
        4. Fill cover letter / additional info textarea.
        5. Fill social/portfolio links.
        6. Fill custom questions via div.application-question loop.
        7. Accept consent checkboxes.
        8. Click submit.
        9. Detect confirmation page.
        """
        from core.applicator import (
            _get_answers, _check_consent_checkboxes, _generate_cover_letter,
        )

        page = self._page
        job_id = self.job_hash[:8]
        answers = _get_answers()

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

        page.wait_for_timeout(1500)

        # ── Click Apply if on a listing page ─────────────────────────────────
        self._click_lever_apply_button(page, job_id)
        page.wait_for_timeout(2000)

        shot = self._safe_screenshot("lv_form_start")
        if shot:
            self._screenshots.append(shot)

        # ── Core fields ───────────────────────────────────────────────────────
        full_name = f"{answers.get('first_name', '')} {answers.get('last_name', '')}".strip()
        self._fill_input(LV["name"],  full_name,               "name")
        self._fill_input(LV["email"], answers.get("email", ""), "email")
        self._fill_input(LV["phone"], answers.get("phone", ""), "phone")

        # ── Resume upload ─────────────────────────────────────────────────────
        if cv_path:
            self._upload_file(LV["resume"], str(cv_path), "resume")

        # ── Cover letter ──────────────────────────────────────────────────────
        if _visible(page, LV["cover_letter"]):
            self._fill_textarea(LV["cover_letter"], self._cover_letter, "cover_letter")

        # ── Social / portfolio links ──────────────────────────────────────────
        social_map = {
            "linkedin":  ("linkedin_url", LV["linkedin"]),
            "github":    ("github",       LV["github"]),
            "portfolio": ("website",      LV["portfolio"]),
            "twitter":   ("twitter",      LV["twitter"]),
        }
        for field_name, (answer_key, sel) in social_map.items():
            if _visible(page, sel) and answers.get(answer_key):
                self._fill_input(sel, answers[answer_key], field_name)

        # ── Custom questions ──────────────────────────────────────────────────
        self._fill_lever_custom_questions(page, answers, job_id)

        # ── Consent checkboxes ────────────────────────────────────────────────
        _check_consent_checkboxes(page, 1)

        page.wait_for_timeout(500)

        # ── CAPTCHA check ─────────────────────────────────────────────────────
        if _dom_detect_captcha(page):
            from core.verifier import request_human_intervention
            request_human_intervention(
                self.job_hash, self.company, self.apply_url,
                "CAPTCHA detected on Lever form — please solve it and send DONE",
            )
            shot2 = self._safe_screenshot("lv_captcha")
            return AdapterResult.need_human(
                "CAPTCHA detected", screenshot_path=shot2
            )

        shot_pre = self._safe_screenshot("lv_pre_submit")
        if shot_pre:
            self._screenshots.append(shot_pre)

        # ── Submit ────────────────────────────────────────────────────────────
        if not self.auto_submit:
            return AdapterResult.fail(
                "failed",
                "auto_submit=False — stopping before final submit",
                screenshot_path=shot_pre,
            )

        submitted = self._click_lever_submit(page, job_id)
        page.wait_for_timeout(4000)

        shot_final = self._safe_screenshot("lv_after_submit")
        if shot_final:
            self._screenshots.append(shot_final)

        # ── Confirm success ───────────────────────────────────────────────────
        if self._detect_lever_success(page):
            return AdapterResult.ok(
                "success",
                screenshot_path=shot_final,
                metadata={"submitted": submitted, "platform": "lever"},
            )

        # Vision fallback for confirmation detection
        if shot_final:
            state = self._vision_classify_state(shot_final)
            if state.kind == "success":
                return AdapterResult.ok("success", screenshot_path=shot_final,
                                        metadata={"submitted": submitted})

        if submitted:
            logger.warning(f"[{job_id}] Lever submit clicked but confirmation not detected")
            return AdapterResult.ok(
                "success",
                screenshot_path=shot_final,
                metadata={"submitted": True, "confirmed": False},
            )

        return AdapterResult.fail(
            "failed", "Lever submit button not found", screenshot_path=shot_pre
        )

    def _fill_lever_custom_questions(
        self, page: "Page", answers: dict, job_id: str
    ) -> None:
        """Fill div.application-question blocks using label → answer matching."""
        from core.applicator import normalize_field_name, lookup_answer

        try:
            containers = page.locator(_QUESTION_SEL)
            count = containers.count()
            logger.info(f"[{job_id}] Lever: {count} custom question container(s) detected")

            for i in range(count):
                try:
                    q = containers.nth(i)

                    # Extract label text
                    label_text = ""
                    try:
                        lbl = q.locator("label, h4, .field-label").first
                        if lbl.count() > 0:
                            label_text = (lbl.inner_text() or "").strip()
                    except Exception:
                        pass

                    # Skip if already filled by a core selector
                    if any(q.locator(sel).count() > 0 for sel in [
                        'input[name="name"]', 'input[name="email"]',
                        'input[name="phone"]', 'input[name="resume"]',
                        'textarea[name="comments"]', 'textarea[name="coverLetter"]',
                    ]):
                        continue

                    candidate_key = normalize_field_name(label_text)
                    value = lookup_answer(candidate_key, answers, label_text, self._client)

                    if not value:
                        logger.debug(f"[{job_id}] No answer for Lever question: {label_text!r}")
                        continue

                    # Detect field type and fill
                    if q.locator("select").count() > 0:
                        sel_el = q.locator("select").first
                        self._select_option(sel_el, value, label_text)

                    elif q.locator("textarea").count() > 0:
                        ta = q.locator("textarea").first
                        if ta.is_visible():
                            ta.fill(str(value))

                    elif q.locator('input[type="checkbox"]').count() > 0:
                        self._handle_checkbox_group(q, value, label_text)

                    elif q.locator('input[type="radio"]').count() > 0:
                        self._handle_radio_group(q, value, label_text)

                    elif q.locator("input").count() > 0:
                        inp = q.locator(
                            'input:not([type="hidden"]):not([type="file"])'
                        ).first
                        if inp.count() > 0 and inp.is_visible():
                            inp.fill(str(value))

                except Exception as exc:
                    logger.debug(f"[{job_id}] Lever question {i} error: {exc}")

        except Exception as exc:
            logger.warning(f"[{job_id}] Lever custom questions fill error: {exc}")

    def _click_lever_apply_button(self, page: "Page", job_id: str) -> bool:
        """Click Apply/Apply Now if we landed on a job listing page, not the form."""
        apply_texts = [
            "Apply for this job", "Apply Now", "Apply", "Submit application",
        ]
        for text in apply_texts:
            for role in ("button", "link"):
                try:
                    loc = page.get_by_role(role, name=text, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        logger.info(f"[{job_id}] Lever: clicked apply button '{text}'")
                        return True
                except Exception:
                    continue
        return False

    def _click_lever_submit(self, page: "Page", job_id: str) -> bool:
        """Click the Lever submit button. Returns True if clicked."""
        submit_texts = [
            "Submit application", "Submit Application", "Submit", "Send Application",
        ]
        # Try text-based first
        for text in submit_texts:
            try:
                loc = page.get_by_role("button", name=text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info(f"[{job_id}] Lever submit clicked: '{text}'")
                    return True
            except Exception:
                continue
        # Fallback: selector-based
        try:
            loc = page.locator(LV["submit"])
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    el.click()
                    logger.info(f"[{job_id}] Lever submit clicked via selector")
                    return True
        except Exception as exc:
            logger.debug(f"[{job_id}] _click_lever_submit fallback: {exc}")
        return False

    def _detect_lever_success(self, page: "Page") -> bool:
        """DOM-only Lever confirmation detection."""
        try:
            url = page.url.lower()
            if "confirmation" in url or "thanks" in url or "thank" in url:
                return True

            # Lever .thanks container
            try:
                if page.locator("div.thanks, .application-confirmation").count() > 0:
                    return True
            except Exception:
                pass

            # Text-based confirmation
            try:
                body = (page.locator("body").inner_text() or "").lower()
                if any(kw in body for kw in (
                    "thank you for applying", "application submitted",
                    "your application has been received",
                    "we have received your application",
                    "thanks for applying",
                )):
                    return True
            except Exception:
                pass
        except Exception:
            pass
        return False

    # ── Shared field helpers (same pattern as GreenhouseAdapter) ─────────────

    def _fill_textarea(self, selector: str, value: str, field_name: str) -> bool:
        """Fill the first visible textarea matching *selector*."""
        try:
            loc = self._page.locator(selector)
            for i in range(min(loc.count(), 3)):
                el = loc.nth(i)
                if el.is_visible():
                    el.fill(value)
                    logger.debug(f"[{self.job_hash[:8]}] Filled textarea {field_name}")
                    return True
        except Exception as exc:
            logger.debug(f"_fill_textarea({field_name}): {exc}")
        return False

    def _upload_file(self, selector: str, file_path: str, field_name: str) -> bool:
        """Set a file input to *file_path*."""
        try:
            loc = self._page.locator(selector)
            for i in range(min(loc.count(), 3)):
                el = loc.nth(i)
                try:
                    el.set_input_files(file_path)
                    logger.debug(f"[{self.job_hash[:8]}] Uploaded {field_name}: {file_path}")
                    return True
                except Exception:
                    continue
        except Exception as exc:
            logger.debug(f"_upload_file({field_name}): {exc}")
        return False

    def _select_option(self, sel_el, value: str, label: str) -> None:
        """Select the best-matching option in a <select> element."""
        try:
            if not sel_el.is_visible():
                return
            value_lower = str(value).lower()
            options = sel_el.locator("option")
            for i in range(options.count()):
                opt = options.nth(i)
                opt_text = (opt.inner_text() or "").strip().lower()
                opt_val = (opt.get_attribute("value") or "").lower()
                if value_lower in opt_text or value_lower in opt_val or opt_text in value_lower:
                    sel_el.select_option(value=opt.get_attribute("value"))
                    return
            # Fallback: first non-empty option
            for i in range(options.count()):
                opt = options.nth(i)
                if opt.get_attribute("value"):
                    sel_el.select_option(value=opt.get_attribute("value"))
                    return
        except Exception as exc:
            logger.debug(f"_select_option({label}): {exc}")

    def _handle_checkbox_group(self, container, value: str, label: str) -> None:
        """Check checkboxes whose labels match *value*."""
        try:
            value_lower = str(value).lower()
            boxes = container.locator('input[type="checkbox"]')
            for i in range(boxes.count()):
                box = boxes.nth(i)
                box_label = ""
                try:
                    lbl_id = box.get_attribute("id")
                    if lbl_id:
                        lbl = container.locator(f'label[for="{lbl_id}"]').first
                        box_label = (lbl.inner_text() or "").strip().lower()
                except Exception:
                    pass
                if value_lower in box_label or box_label in value_lower:
                    if not box.is_checked():
                        box.check()
        except Exception as exc:
            logger.debug(f"_handle_checkbox_group({label}): {exc}")

    def _handle_radio_group(self, container, value: str, label: str) -> None:
        """Select the radio button whose label best matches *value*."""
        try:
            value_lower = str(value).lower()
            radios = container.locator('input[type="radio"]')
            for i in range(radios.count()):
                radio = radios.nth(i)
                radio_label = ""
                try:
                    lbl_id = radio.get_attribute("id")
                    if lbl_id:
                        lbl = container.locator(f'label[for="{lbl_id}"]').first
                        radio_label = (lbl.inner_text() or "").strip().lower()
                except Exception:
                    pass
                if value_lower in radio_label or radio_label in value_lower:
                    if not radio.is_checked():
                        radio.check()
                    return
        except Exception as exc:
            logger.debug(f"_handle_radio_group({label}): {exc}")

    def _resolve_cv(self):
        """Return the CV path from config."""
        try:
            from config.settings import DATA_DIR
            cv = DATA_DIR / "CV Resume.pdf"
            return cv if cv.exists() else None
        except Exception:
            return None


# ── Self-registration ──────────────────────────────────────────────────────────
register_adapter("lever", LeverAdapter)
