"""GreenhouseAdapter — Greenhouse ATS platform adapter.

Greenhouse Flow
--------------
  navigate → detect page state
  └─ form page → fill_form (single-page, deterministic) → submit → success

Key observations
----------------
- Forms are almost always directly accessible (no login required).
- Greenhouse generates stable IDs for all core fields:
    #first_name, #last_name, #email, #phone
    #resume (file upload), #cover_letter
- Custom questions use a predictable pattern:
    li.custom-field input / textarea / select
- The submit button is always #submit_app or input[type="submit"].
- Confirmation page includes #application-confirmation or "Thank you" h1.

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

_PLATFORM_KEY = "greenhouse"

GREENHOUSE_DOMAINS = (
    "greenhouse.io",
    "grnh.se",          # Greenhouse short-links
)

# Stable Greenhouse field IDs
GH: dict[str, str] = {
    "first_name":   "#first_name",
    "last_name":    "#last_name",
    "email":        "#email",
    "phone":        "#phone",
    "resume":       "#resume, input[name='resume'], input[id*='resume'][type='file']",
    "cover_letter": "#cover_letter, textarea[name='cover_letter']",
    "linkedin":     "input[id*='linkedin' i], input[placeholder*='linkedin' i]",
    "website":      "input[id*='website' i], input[id*='portfolio' i], input[placeholder*='website' i]",
    "github":       "input[id*='github' i], input[placeholder*='github' i]",
    "submit":       "#submit_app, input[type='submit'], button[type='submit']",
}

# Custom questions container — Greenhouse wraps each question in <li class="custom-field">
_CUSTOM_FIELD_SEL = "li.custom-field"


# ── GreenhouseAdapter ─────────────────────────────────────────────────────────

class GreenhouseAdapter(GenericAdapter):
    """Handles boards.greenhouse.io and direct Greenhouse embed URLs.

    Inherits browser infrastructure from GenericAdapter.
    Overrides fill_form() with Greenhouse-specific deterministic selectors.
    """

    name = "greenhouse"

    @classmethod
    def detect(cls, url: str) -> bool:
        return any(d in url for d in GREENHOUSE_DOMAINS)

    # ── Orchestrator interface ────────────────────────────────────────────────

    def plan(self, checkpoint_meta: dict) -> StepResult:
        """Open browser, navigate to apply_url, detect page state.

        Greenhouse forms are always publicly accessible — expect 'form' or
        'apply_button' immediately.  Inherits GenericAdapter.plan() unchanged.
        """
        return super().plan(checkpoint_meta)

    def fill_form(self, checkpoint_meta: dict) -> StepResult:
        """Fill Greenhouse application form with deterministic selectors."""
        try:
            result = self._do_fill_greenhouse_form()
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
            logger.exception(f"[{self.job_hash[:8]}] GreenhouseAdapter.fill_form failed: {exc}")
            shot = self._safe_screenshot("gh_fill_crash")
            return StepResult(ApplyState.FAILED, success=False,
                              error=str(exc), screenshot_path=shot)

    # ── Greenhouse-specific form filling ──────────────────────────────────────

    def _do_fill_greenhouse_form(self) -> AdapterResult:
        """Deterministic Greenhouse form fill.

        Steps:
        1. Fill core fields (name, email, phone) via stable IDs.
        2. Upload resume via #resume.
        3. Fill cover letter if present.
        4. Fill social/portfolio links.
        5. Fill custom questions via li.custom-field loop.
        6. Accept consent checkboxes.
        7. Click submit.
        8. Detect confirmation page.
        """
        from core.applicator import (
            _get_answers, _check_consent_checkboxes, _generate_cover_letter,
            _screenshot as _app_screenshot,
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
        shot = self._safe_screenshot("gh_form_start")
        if shot:
            self._screenshots.append(shot)

        # ── Core fields ───────────────────────────────────────────────────────
        self._fill_input(GH["first_name"], answers.get("first_name", ""), "first_name")
        self._fill_input(GH["last_name"],  answers.get("last_name", ""),  "last_name")
        self._fill_input(GH["email"],      answers.get("email", ""),      "email")
        self._fill_input(GH["phone"],      answers.get("phone", ""),      "phone")

        # ── Resume upload ─────────────────────────────────────────────────────
        if cv_path:
            self._upload_file(GH["resume"], str(cv_path), "resume")

        # ── Cover letter ──────────────────────────────────────────────────────
        if _visible(page, GH["cover_letter"]):
            self._fill_textarea(GH["cover_letter"], self._cover_letter, "cover_letter")

        # ── Social / portfolio links ──────────────────────────────────────────
        if _visible(page, GH["linkedin"]):
            self._fill_input(GH["linkedin"], answers.get("linkedin_url", ""), "linkedin")
        if _visible(page, GH["website"]):
            self._fill_input(GH["website"], answers.get("website", ""), "website")
        if _visible(page, GH["github"]):
            self._fill_input(GH["github"], answers.get("github", ""), "github")

        # ── Custom questions ──────────────────────────────────────────────────
        self._fill_greenhouse_custom_questions(page, answers, job_id)

        # ── Consent checkboxes ────────────────────────────────────────────────
        _check_consent_checkboxes(page, 1)

        page.wait_for_timeout(500)

        # ── CAPTCHA check ─────────────────────────────────────────────────────
        if _dom_detect_captcha(page):
            from core.verifier import request_human_intervention
            request_human_intervention(
                self.job_hash, self.company, self.apply_url,
                "CAPTCHA detected on Greenhouse form — please solve it and send DONE",
            )
            shot2 = self._safe_screenshot("gh_captcha")
            return AdapterResult.need_human(
                "CAPTCHA detected", screenshot_path=shot2
            )

        shot_pre = self._safe_screenshot("gh_pre_submit")
        if shot_pre:
            self._screenshots.append(shot_pre)

        # ── Submit ────────────────────────────────────────────────────────────
        if not self.auto_submit:
            return AdapterResult.fail(
                "failed",
                "auto_submit=False — stopping before final submit",
                screenshot_path=shot_pre,
            )

        submitted = self._click_submit(page, job_id)
        page.wait_for_timeout(4000)

        shot_final = self._safe_screenshot("gh_after_submit")
        if shot_final:
            self._screenshots.append(shot_final)

        # ── Confirm success ───────────────────────────────────────────────────
        if self._detect_greenhouse_success(page):
            return AdapterResult.ok(
                "success",
                screenshot_path=shot_final,
                metadata={"submitted": submitted, "platform": "greenhouse"},
            )

        # Vision fallback for success confirmation
        if shot_final:
            state = self._vision_classify_state(shot_final)
            if state.kind == "success":
                return AdapterResult.ok("success", screenshot_path=shot_final,
                                        metadata={"submitted": submitted})

        if submitted:
            logger.warning(f"[{job_id}] Greenhouse submit clicked but confirmation not detected")
            return AdapterResult.ok(
                "success",
                screenshot_path=shot_final,
                metadata={"submitted": True, "confirmed": False},
            )

        return AdapterResult.fail(
            "failed", "Greenhouse submit button not found", screenshot_path=shot_pre
        )

    def _fill_greenhouse_custom_questions(
        self, page: "Page", answers: dict, job_id: str
    ) -> None:
        """Fill li.custom-field question blocks using label → answer matching."""
        from core.applicator import normalize_field_name, lookup_answer

        try:
            fields = page.locator(_CUSTOM_FIELD_SEL)
            count = fields.count()
            logger.info(f"[{job_id}] Greenhouse: {count} custom field(s) detected")

            for i in range(count):
                try:
                    field_el = fields.nth(i)

                    # Extract label text for answer lookup
                    label_text = ""
                    try:
                        label_el = field_el.locator("label").first
                        if label_el.count() > 0:
                            label_text = (label_el.inner_text() or "").strip()
                    except Exception:
                        pass

                    candidate_key = normalize_field_name(label_text)
                    value = lookup_answer(candidate_key, answers, label_text, self._client)

                    if not value:
                        logger.debug(f"[{job_id}] No answer for custom field: {label_text!r}")
                        continue

                    # Select field type and fill
                    if field_el.locator("select").count() > 0:
                        sel_el = field_el.locator("select").first
                        self._select_option(sel_el, value, label_text)

                    elif field_el.locator("textarea").count() > 0:
                        ta = field_el.locator("textarea").first
                        if ta.is_visible():
                            ta.fill(str(value))

                    elif field_el.locator('input[type="checkbox"]').count() > 0:
                        self._handle_checkbox_group(field_el, value, label_text)

                    elif field_el.locator('input[type="radio"]').count() > 0:
                        self._handle_radio_group(field_el, value, label_text)

                    elif field_el.locator("input").count() > 0:
                        inp = field_el.locator(
                            'input:not([type="hidden"]):not([type="file"])'
                        ).first
                        if inp.count() > 0 and inp.is_visible():
                            inp.fill(str(value))

                except Exception as exc:
                    logger.debug(f"[{job_id}] Custom field {i} error: {exc}")

        except Exception as exc:
            logger.warning(f"[{job_id}] Custom questions fill error: {exc}")

    def _select_option(self, sel_el, value: str, label: str) -> None:
        """Select best-matching option in a <select> element."""
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
            # Fall back to first non-empty option
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

    def _click_submit(self, page: "Page", job_id: str) -> bool:
        """Click the Greenhouse submit button. Returns True if clicked."""
        try:
            loc = page.locator(GH["submit"])
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    el.click()
                    logger.info(f"[{job_id}] Greenhouse submit clicked")
                    return True
        except Exception as exc:
            logger.debug(f"[{job_id}] _click_submit: {exc}")
        return False

    def _detect_greenhouse_success(self, page: "Page") -> bool:
        """DOM-only Greenhouse confirmation detection."""
        try:
            url = page.url.lower()
            for kw in ("confirmation", "thank", "success", "submitted"):
                if kw in url:
                    return True

            # Greenhouse confirmation div
            try:
                if page.locator("#application-confirmation").count() > 0:
                    return True
            except Exception:
                pass

            # Text-based confirmation
            try:
                body = (page.locator("body").inner_text() or "").lower()
                if any(kw in body for kw in (
                    "thank you for applying", "application received",
                    "your application has been submitted", "we received your application",
                    "application submitted",
                )):
                    return True
            except Exception:
                pass
        except Exception:
            pass
        return False

    def _resolve_cv(self):
        """Return the CV path from config."""
        from pathlib import Path
        try:
            from config.settings import DATA_DIR
            cv = DATA_DIR / "CV Resume.pdf"
            return cv if cv.exists() else None
        except Exception:
            return None


# ── Self-registration ──────────────────────────────────────────────────────────
register_adapter("greenhouse", GreenhouseAdapter)
