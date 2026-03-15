"""Auto form-filling and job application via Playwright + Claude Vision.

Architecture:
- Central answer database: data/default_answers.yaml
- Field name normalization: maps any label to a canonical key
- 4-strategy fill cascade for required fields
- Dropdown / radio / checkbox handling
- Pre-submit validation: refuse to submit if required fields empty
- Multi-page form loop with Next/Submit detection
- Claude Vision for field identification and page state analysis

Supports: Breezy, Greenhouse, Lever, Ashby, Workday, SmartRecruiters, etc.
"""

import os
import re
import json
import base64
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

import anthropic
import yaml
from loguru import logger
from playwright.sync_api import sync_playwright, Page

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
SCREENSHOTS_DIR = ROOT / "data" / "screenshots"
CV_PATH = ROOT / "data" / "CV Resume.pdf"
PROFILE_PATH = ROOT / "config" / "profile.yaml"
ANSWERS_PATH = ROOT / "data" / "default_answers.yaml"

# ── ATS platform keywords for cache key extraction ────────────────────────────
ATS_KEYWORDS = [
    "comeet", "greenhouse", "lever", "ashby", "workday", "smartrecruiters",
    "breezyhr", "jazz", "bamboohr", "icims", "taleo", "jobvite",
    "recruitee", "personio", "freshteam", "applytojob", "dover",
]

# ── ATS Field Memory helpers ──────────────────────────────────────────────────

def _extract_ats_key(url: str) -> str | None:
    """Extract ATS platform identifier from a URL hostname."""
    host = urlparse(url).hostname or ""
    host = host.lower()
    for kw in ATS_KEYWORDS:
        if kw in host:
            return kw
    return None


def _get_cached_fields(ats_key: str) -> dict | None:
    """Look up cached field mappings for an ATS. Returns None if not cached or untrusted."""
    from db.database import get_session
    from db.models import ATSFieldMemory
    session = get_session()
    try:
        mem = session.query(ATSFieldMemory).filter_by(ats_key=ats_key).first()
        if mem and mem.success_count >= 2:
            logger.info(f"Using cached ATS mapping for {ats_key} (success_count={mem.success_count})")
            return mem.field_mappings
        return None
    finally:
        session.close()


def _save_ats_fields(ats_key: str, field_mappings: dict):
    """Upsert field mappings for an ATS platform after a successful application."""
    from db.database import get_session
    from db.models import ATSFieldMemory
    session = get_session()
    try:
        mem = session.query(ATSFieldMemory).filter_by(ats_key=ats_key).first()
        if mem:
            mem.field_mappings = field_mappings
            mem.success_count += 1
            mem.last_used = datetime.now(timezone.utc)
        else:
            mem = ATSFieldMemory(
                ats_key=ats_key,
                field_mappings=field_mappings,
                success_count=1,
                last_used=datetime.now(timezone.utc),
            )
            session.add(mem)
        session.commit()
        logger.info(f"ATS field memory saved for {ats_key}")
    finally:
        session.close()


def _resolve_cv_path(cv_variant: str | None = None) -> Path:
    """Resolve the CV file path, supporting multiple CV versions."""
    if cv_variant:
        variant_path = ROOT / "data" / f"{cv_variant}.pdf"
        if variant_path.exists():
            logger.info(f"Using CV variant: {cv_variant}")
            return variant_path
        logger.warning(f"CV variant '{cv_variant}' not found at {variant_path}, using default")
    return CV_PATH


# ── Consent / checkbox keywords ───────────────────────────────────────────────
CONSENT_KEYWORDS = [
    "privacy", "consent", "agree", "terms", "data processing",
    "policy", "acknowledge", "authorize", "gdpr", "i accept",
    "i agree", "terms and conditions", "terms of service",
    "data protection", "i confirm",
]

# ── Common button texts for ATS detection ─────────────────────────────────────
APPLY_BUTTON_TEXTS = [
    "Apply Now",
    "Apply for this job",
    "Apply for this position",
    "Apply to this job",
    "Apply",
    "Submit Application",
    "Start Application",
    "I'm interested",
]

NEXT_BUTTON_TEXTS = [
    "Next",
    "Continue",
    "Save and Continue",
    "Save & Continue",
    "Proceed",
    "Next Step",
]

SUBMIT_BUTTON_TEXTS = [
    "Submit Application",
    "Submit",
    "Apply",
    "Send Application",
    "Complete Application",
    "Finish",
]

# ── Form field selectors (to detect if a form is present) ─────────────────────
FORM_FIELD_SELECTORS = [
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"])',
    "textarea",
    "select",
    'input[type="file"]',
]

# ── Field name normalization map ──────────────────────────────────────────────
# Maps common label variations to canonical keys in default_answers.yaml
FIELD_NORMALIZATION = {
    # Name
    "full name": "full_name", "name": "full_name", "your name": "full_name",
    "candidate name": "full_name",
    "first name": "first_name", "given name": "first_name", "forename": "first_name",
    "last name": "last_name", "surname": "last_name", "family name": "last_name",

    # Contact
    "email": "email", "email address": "email", "e-mail": "email",
    "email id": "email", "your email": "email",
    "phone": "phone", "phone number": "phone", "telephone": "phone",
    "mobile": "phone", "mobile number": "phone", "contact number": "phone",
    "cell phone": "phone", "your phone": "phone",

    # Location
    "location": "location", "city": "city", "town": "city",
    "address": "address", "street address": "address",
    "country": "country", "state": "state", "province": "state",
    "zip": "zip_code", "zip code": "zip_code", "postal code": "zip_code",

    # Online
    "linkedin": "linkedin", "linkedin url": "linkedin", "linkedin profile": "linkedin",
    "linkedin profile url": "linkedin",
    "github": "github", "github url": "github", "github profile": "github",
    "website": "website", "personal website": "website", "portfolio": "portfolio",
    "portfolio url": "portfolio", "personal url": "website", "url": "website",
    "portfolio website": "portfolio",

    # Education
    "university": "university", "school": "university", "college": "university",
    "institution": "university", "school name": "university",
    "university name": "university", "alma mater": "university",
    "degree": "degree", "degree type": "education_level",
    "education level": "education_level", "highest education": "education_level",
    "education": "education_level",
    "major": "major", "field of study": "field_of_study",
    "area of study": "field_of_study", "concentration": "major",
    "gpa": "gpa", "grade point average": "gpa", "grade": "gpa",
    "cumulative gpa": "gpa", "cgpa": "gpa",
    "graduation year": "graduation_year", "expected graduation": "expected_graduation",
    "graduation date": "graduation_year", "year of graduation": "graduation_year",
    "current year": "current_year_of_study", "year of study": "current_year_of_study",
    "academic year": "current_year_of_study",

    # Work authorization
    "work authorization": "work_authorization",
    "authorized to work": "authorized_to_work",
    "are you authorized to work": "authorized_to_work",
    "legally authorized": "legally_authorized",
    "visa": "visa_sponsorship_required",
    "visa sponsorship": "visa_sponsorship_required",
    "require sponsorship": "require_sponsorship",
    "do you require sponsorship": "require_sponsorship",
    "sponsorship": "require_sponsorship",
    "will you now or in the future require sponsorship": "require_sponsorship",

    # Employment
    "salary": "salary", "salary expectation": "salary_expectation",
    "expected salary": "expected_salary", "desired salary": "desired_salary",
    "salary expectations": "salary_expectation",
    "compensation": "salary_expectation", "compensation expectations": "salary_expectation",
    "current company": "current_company", "current employer": "current_company",
    "current title": "current_title", "current job title": "current_title",
    "years of experience": "years_of_experience",
    "experience": "years_of_experience", "total experience": "years_of_experience",
    "notice period": "notice_period",

    # Availability
    "availability": "availability", "available start date": "available_start_date",
    "start date": "start_date", "earliest start date": "start_date",
    "when can you start": "start_date",

    # Languages / Skills
    "languages": "languages", "spoken languages": "languages",
    "language proficiency": "languages",
    "skills": "skills", "technical skills": "skills",
    "programming languages": "programming_languages",

    # About
    "about me": "about_me", "about yourself": "about_me", "tell us about yourself": "about_me",
    "summary": "summary", "professional summary": "summary",
    "introduction": "about_me", "bio": "about_me",

    # Cover letter
    "cover letter": "cover_letter", "letter": "cover_letter",
    "motivation letter": "cover_letter", "message": "cover_letter",
    "additional information": "about_me", "comments": "about_me",

    # CV / Resume
    "cv": "cv_upload", "resume": "cv_upload", "upload cv": "cv_upload",
    "upload resume": "cv_upload", "attach resume": "cv_upload",
    "attach cv": "cv_upload",

    # Diversity
    "gender": "gender", "ethnicity": "ethnicity", "race": "race",
    "disability": "disability", "veteran": "veteran", "veteran status": "veteran",

    # Misc yes/no
    "relocate": "relocate", "willing to relocate": "willing_to_relocate",
    "open to relocation": "willing_to_relocate",
    "background check": "background_check",
    "over 18": "over_18", "are you over 18": "over_18",
    "are you at least 18": "over_18",
    "felony": "felony",

    # Referral
    "referral": "referral", "referred by": "referred_by",
    "how did you hear": "how_did_you_hear",
    "how did you hear about us": "how_did_you_hear",
    "how did you find this job": "how_did_you_hear",
    "source": "source",

    # Military
    "military service": "military_service", "military": "military_service",
}

# Keys that correspond to yes/no radio questions
YES_NO_KEYS = {
    "work_authorization", "authorized_to_work", "legally_authorized",
    "visa_sponsorship_required", "require_sponsorship",
    "relocate", "willing_to_relocate", "background_check",
    "drug_test", "over_18", "felony", "citizen", "remote_work",
    "disability", "veteran",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Answer Database & Field Normalization
# ═══════════════════════════════════════════════════════════════════════════════

def _load_answers() -> dict:
    """Load the central answer database from YAML."""
    with open(ANSWERS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_profile() -> dict:
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# Cached at module level (loaded on first use)
_answers_cache: dict | None = None


def _get_answers() -> dict:
    """Get cached answers, loading from disk on first call."""
    global _answers_cache
    if _answers_cache is None:
        _answers_cache = _load_answers()
    return _answers_cache


def normalize_field_name(label: str) -> str:
    """Normalize a form field label to a canonical key.

    Steps:
    1. Lowercase, strip whitespace and asterisks (required markers)
    2. Look up in FIELD_NORMALIZATION map
    3. Try partial/fuzzy matching if exact match fails
    4. Return the label as-is (snake_case) if no mapping found
    """
    if not label:
        return "other"

    # Clean: lowercase, strip *, trim
    cleaned = re.sub(r'[*\u200b\xa0]', '', label).strip().lower()
    cleaned = re.sub(r'\s+', ' ', cleaned)

    # Exact match
    if cleaned in FIELD_NORMALIZATION:
        return FIELD_NORMALIZATION[cleaned]

    # Partial match: check if any normalization key is contained in the label
    for key, canonical in FIELD_NORMALIZATION.items():
        if key in cleaned:
            return canonical

    # Check if the label contains any normalization key
    for key, canonical in FIELD_NORMALIZATION.items():
        if cleaned in key:
            return canonical

    # Convert to snake_case as fallback
    fallback = re.sub(r'[^a-z0-9]+', '_', cleaned).strip('_')
    return fallback or "other"


def lookup_answer(field_label: str, candidate_field: str = "",
                  field_type: str = "text") -> str:
    """Look up the answer for a field from the central answer database.

    Priority:
    1. candidate_field from Claude Vision (if it maps to an answer)
    2. Normalized field label
    3. Smart defaults based on field type
    """
    answers = _get_answers()

    # 1. Try candidate_field directly (from Claude Vision analysis)
    if candidate_field and candidate_field in answers:
        return str(answers[candidate_field])

    # 2. Normalize the label and look up
    normalized = normalize_field_name(field_label)
    if normalized in answers:
        return str(answers[normalized])

    # 3. Smart defaults based on field type
    if field_type == "textarea":
        return str(answers.get("about_me", ""))
    if field_type == "select":
        return ""  # Will be handled by dropdown logic
    if field_type == "radio":
        return ""  # Will be handled by radio logic

    return ""


# ═══════════════════════════════════════════════════════════════════════════════
#  Utilities
# ═══════════════════════════════════════════════════════════════════════════════

def _step(step_num: int, msg: str):
    """Print a formatted step message."""
    print(f"[STEP {step_num}] {msg}")
    logger.info(f"[STEP {step_num}] {msg}")


def _screenshot(page: Page, job_id: str, name: str) -> Path:
    """Take a screenshot and save it."""
    shot_dir = SCREENSHOTS_DIR / job_id
    shot_dir.mkdir(parents=True, exist_ok=True)
    path = shot_dir / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def _image_to_base64(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _parse_json_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON from Claude response."""
    text = raw.strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON object from within the text
        import re
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
#  Claude Vision helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ask_claude_vision(client: anthropic.Anthropic, screenshot_b64: str, prompt: str) -> str:
    """Send a screenshot to Claude Vision and get a text response."""
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": screenshot_b64,
                    },
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }],
    )
    return message.content[0].text.strip()


def _identify_fields(client: anthropic.Anthropic, screenshot_path: Path) -> dict:
    """Use Claude Vision to identify form fields from a screenshot."""
    b64 = _image_to_base64(screenshot_path)

    prompt = """Analyze this job application form screenshot. Identify ALL visible form fields.

For each field, return a JSON array with objects like:
{
  "label": "the visible label text",
  "type": "text|email|tel|textarea|file|select|radio|checkbox|url",
  "candidate_field": "first_name|last_name|full_name|email|phone|linkedin|github|university|degree|gpa|graduation_year|year_in_degree|cv_upload|cover_letter|languages|availability|website|salary|work_authorization|visa_sponsorship|location|city|country|about_me|skills|other",
  "required": true/false,
  "placeholder": "any placeholder text visible",
  "options": ["option1", "option2"]
}

The "options" field is only for select/radio fields - list the visible options.

Also identify any buttons:
- "next_button": true if there's a Next/Continue button (multi-page form)
- "submit_button": true if there's a Submit/Apply button

Return ONLY valid JSON in this format:
{
  "fields": [...],
  "next_button": true/false,
  "submit_button": true/false,
  "next_button_text": "the text on the next button if present",
  "submit_button_text": "the text on the submit button if present"
}"""

    raw = _ask_claude_vision(client, b64, prompt)
    return _parse_json_response(raw)


def _generate_cover_letter(client: anthropic.Anthropic, job_title: str, company: str,
                           job_description: str, user_instruction: str = "") -> str:
    """Generate a short, tailored cover letter using Claude.

    user_instruction: optional free-text guidance from the user
    (e.g. "emphasize Docker experience", "mention my open-source project").
    """
    answers = _get_answers()
    instruction_line = (
        f"\nUser instruction: \"{user_instruction}\" — incorporate this emphasis into the letter."
        if user_instruction else ""
    )
    prompt = f"""Write a short cover letter (60-90 words) for this job application.
Use this exact style and tone as a reference:

"Hi,

I'm a third-year Software Engineering student at Bar Ilan University (GPA 88) and I'm currently looking for a software engineering internship. I have experience with C++, Python, and backend development, along with web development using React and Node.js through several academic and personal projects.

I'm especially interested in opportunities where I can contribute to real systems while continuing to learn from experienced engineers.

Thanks for your consideration,
Elad Damti"

Now write a similar cover letter tailored to this specific job:

Job: {job_title} at {company}
Description: {job_description[:1500]}

Candidate skills: C++, Python, Backend, REST APIs, React, Node.js, MongoDB, Docker, Linux
Military: IDF C4I — Networking Instructor & Team Lead
{instruction_line}
Rules:
- Keep the same casual, honest, student tone as the reference
- Start with "Hi," on its own line
- End with "Thanks for your consideration,\\nElad Damti"
- Mention 1-2 skills that are specifically relevant to THIS job
- One short paragraph about background, one short sentence about why this role interests you
- Do NOT use corporate language, buzzwords, or AI-sounding phrases
- Do NOT say "I am writing to express" or "I am excited to apply" or "Dear Hiring Team"
- Keep it under 90 words (excluding greeting and sign-off)
"""
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ═══════════════════════════════════════════════════════════════════════════════
#  Page interaction: detect apply button, detect forms, popups
# ═══════════════════════════════════════════════════════════════════════════════

def _has_visible_form(page: Page) -> bool:
    """Check if the page currently has visible form fields."""
    for sel in FORM_FIELD_SELECTORS:
        try:
            visible = page.locator(sel)
            if visible.count() > 0:
                for i in range(min(visible.count(), 5)):
                    if visible.nth(i).is_visible():
                        return True
        except Exception:
            continue
    return False


def _wait_for_form(page: Page, step: int, timeout_ms: int = 10000) -> int:
    """Wait for form fields to appear on the page after clicking Apply."""
    _step(step, "Waiting for application form to load...")
    combined_selector = ", ".join(FORM_FIELD_SELECTORS)
    try:
        page.wait_for_selector(combined_selector, state="visible", timeout=timeout_ms)
        step += 1
        _step(step, "Application form loaded -- form fields detected")
    except Exception:
        step += 1
        _step(step, "Timed out waiting for form fields -- continuing anyway")
    page.wait_for_timeout(1500)
    return step


def _dismiss_popups(page: Page):
    """Try to close cookie banners, modals, etc. that might block the form."""
    dismiss_selectors = [
        'button:has-text("Accept")', 'button:has-text("Accept All")',
        'button:has-text("Got it")', 'button:has-text("Close")',
        'button:has-text("Dismiss")', '[aria-label="Close"]',
        '[aria-label="close"]', 'button.close',
        '.cookie-banner button', '#onetrust-accept-btn-handler',
    ]
    for sel in dismiss_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


# ═══════════════════════════════════════════════════════════════════════════════
#  Field filling: checkboxes, dropdowns, radio buttons, text fields
# ═══════════════════════════════════════════════════════════════════════════════

def _is_consent_field(field: dict) -> bool:
    """Check if a field is a consent/privacy/terms checkbox."""
    label_lower = (field.get("label", "") or "").lower()
    return field.get("type") == "checkbox" and any(
        kw in label_lower for kw in CONSENT_KEYWORDS
    )


def _check_consent_checkboxes(page: Page, step_num: int) -> int:
    """Find and check all consent/privacy/terms checkboxes on the page."""
    checked_any = False

    # Strategy 1: label text matching
    for kw in CONSENT_KEYWORDS:
        try:
            loc = page.get_by_label(kw, exact=False)
            for i in range(min(loc.count(), 3)):
                el = loc.nth(i)
                if el.is_visible() and not el.is_checked():
                    el.check()
                    _step(step_num, f"Checking consent checkbox: \"{kw}\"")
                    checked_any = True
                    step_num += 1
        except Exception:
            continue

    # Strategy 2: DOM traversal
    if not checked_any:
        try:
            checkboxes = page.locator('input[type="checkbox"]')
            for i in range(min(checkboxes.count(), 10)):
                cb = checkboxes.nth(i)
                if not cb.is_visible() or cb.is_checked():
                    continue
                parent_text = ""
                try:
                    parent_text = cb.locator("xpath=..").inner_text().lower()
                except Exception:
                    pass
                try:
                    cb_id = cb.get_attribute("id")
                    if cb_id:
                        label_el = page.locator(f'label[for="{cb_id}"]')
                        if label_el.count() > 0:
                            parent_text += " " + label_el.first.inner_text().lower()
                except Exception:
                    pass
                if any(kw in parent_text for kw in CONSENT_KEYWORDS):
                    cb.check()
                    _step(step_num, f"Checking consent checkbox (DOM): \"{parent_text[:50]}\"")
                    checked_any = True
                    step_num += 1
        except Exception:
            pass

    if not checked_any:
        _step(step_num, "No consent checkboxes found on page")
        step_num += 1
    return step_num


def _fill_dropdown(page: Page, field: dict, value: str, step_num: int) -> tuple[bool, int]:
    """Handle select/dropdown fields.

    Strategy:
    1. Try to match option text with the answer value
    2. If no match, select first non-empty option

    Returns (filled, step_num).
    """
    label = field.get("label", "")
    filled = False

    # Try to find the select element
    select_loc = None
    strategies = [
        lambda: page.get_by_label(label, exact=False),
        lambda: page.locator(f'select[name*="{normalize_field_name(label)}" i]'),
        lambda: page.locator(f'select[id*="{normalize_field_name(label)}" i]'),
        lambda: page.locator(f'select[aria-label*="{label}" i]'),
    ]
    for fn in strategies:
        try:
            loc = fn()
            if loc.count() > 0 and loc.first.is_visible():
                tag = loc.first.evaluate("el => el.tagName")
                if tag == "SELECT":
                    select_loc = loc.first
                    break
        except Exception:
            continue

    if not select_loc:
        # Try any visible select near a label
        try:
            selects = page.locator("select")
            for i in range(min(selects.count(), 10)):
                sel = selects.nth(i)
                if sel.is_visible():
                    # Check if this select's label matches
                    try:
                        sel_id = sel.get_attribute("id") or ""
                        sel_name = sel.get_attribute("name") or ""
                        if (normalize_field_name(label) in sel_id.lower() or
                                normalize_field_name(label) in sel_name.lower()):
                            select_loc = sel
                            break
                    except Exception:
                        pass
        except Exception:
            pass

    if not select_loc:
        return False, step_num

    try:
        # Get all options
        options = select_loc.locator("option").all_text_contents()
        option_values = []
        try:
            count = select_loc.locator("option").count()
            for i in range(count):
                val = select_loc.locator("option").nth(i).get_attribute("value") or ""
                option_values.append(val)
        except Exception:
            option_values = options

        # Strategy 1: Match answer value to option text (case-insensitive)
        value_lower = value.lower() if value else ""
        for idx, opt_text in enumerate(options):
            if value_lower and value_lower in opt_text.lower():
                if idx < len(option_values) and option_values[idx]:
                    select_loc.select_option(value=option_values[idx])
                else:
                    select_loc.select_option(label=opt_text)
                _step(step_num, f"Dropdown \"{label}\" -> \"{opt_text}\" done")
                filled = True
                break

        # Strategy 2: Select first non-empty, non-placeholder option
        if not filled:
            for idx, opt_text in enumerate(options):
                stripped = opt_text.strip().lower()
                if stripped and stripped not in ("", "select", "select...", "choose",
                                                 "choose...", "--", "---", "please select",
                                                 "select one", "select an option"):
                    if idx < len(option_values) and option_values[idx]:
                        select_loc.select_option(value=option_values[idx])
                    else:
                        select_loc.select_option(label=opt_text)
                    _step(step_num, f"Dropdown \"{label}\" -> \"{opt_text}\" (first valid option)")
                    filled = True
                    break

    except Exception as e:
        _step(step_num, f"Dropdown \"{label}\" failed: {e}")

    return filled, step_num + 1


def _fill_radio(page: Page, field: dict, value: str, step_num: int) -> tuple[bool, int]:
    """Handle radio button groups (yes/no questions, multiple choice).

    Returns (filled, step_num).
    """
    label = field.get("label", "")
    normalized = normalize_field_name(label)
    answers = _get_answers()

    # Determine the answer
    answer = value or answers.get(normalized, "")
    if not answer and normalized in YES_NO_KEYS:
        answer = answers.get(normalized, "Yes")

    if not answer:
        return False, step_num

    answer_lower = answer.lower()
    filled = False

    # Strategy 1: Find radio buttons by group name or nearby label
    try:
        # Get all radio inputs
        radios = page.locator('input[type="radio"]')
        count = radios.count()

        for i in range(min(count, 20)):
            radio = radios.nth(i)
            if not radio.is_visible():
                continue

            # Get the radio's label text
            radio_label = ""
            try:
                radio_id = radio.get_attribute("id")
                if radio_id:
                    label_el = page.locator(f'label[for="{radio_id}"]')
                    if label_el.count() > 0:
                        radio_label = label_el.first.inner_text().strip().lower()
            except Exception:
                pass

            if not radio_label:
                try:
                    radio_label = radio.locator("xpath=..").inner_text().strip().lower()
                except Exception:
                    pass

            # Check if this radio's label matches the answer
            radio_value = (radio.get_attribute("value") or "").lower()

            if (answer_lower in radio_label or answer_lower == radio_value or
                    radio_label in answer_lower):
                radio.check()
                _step(step_num, f"Radio \"{label}\" -> \"{radio_label or radio_value}\" done")
                filled = True
                break

    except Exception as e:
        _step(step_num, f"Radio \"{label}\" failed: {e}")

    return filled, step_num + 1


def _fill_field(page: Page, field: dict, value: str, step_num: int,
                cover_letter: str = "") -> tuple[bool, int]:
    """Fill a single form field with robust fallback for required fields.

    Strategy order:
    1. Label-based selector
    2. Placeholder-based selector
    3. Name/id/aria-label attribute selectors
    4. Fallback by element type

    Returns (filled: bool, updated step_num).
    """
    label = field.get("label", "")
    field_type = field.get("type", "text")
    is_required = field.get("required", False)
    candidate_field = field.get("candidate_field", "other")
    normalized = normalize_field_name(label)

    # ── Resolve value from answer database if not provided ────────────────
    if not value:
        value = lookup_answer(label, candidate_field, field_type)

    # Special: cover_letter always uses the generated one
    if candidate_field == "cover_letter" or normalized == "cover_letter":
        if cover_letter:
            value = cover_letter

    # ── Log the normalization ─────────────────────────────────────────────
    if is_required:
        _step(step_num, f"Required field detected: \"{label}\"")
        _step(step_num, f"Normalized field name: {normalized}")
        if value:
            _step(step_num, "Value found in default_answers.yaml")
        else:
            _step(step_num, "No value in default_answers.yaml -- will attempt fallback")

    # ── Consent checkbox handling ─────────────────────────────────────────
    if _is_consent_field(field):
        _step(step_num, f"Consent checkbox detected: \"{label}\"")
        try:
            loc = page.get_by_label(label, exact=False)
            if loc.count() > 0 and loc.first.is_visible():
                if not loc.first.is_checked():
                    loc.first.check()
                _step(step_num + 1, f"Checking checkbox done")
                return True, step_num + 2
        except Exception:
            pass
        step_num = _check_consent_checkboxes(page, step_num)
        return True, step_num

    # ── Regular checkbox ──────────────────────────────────────────────────
    if field_type == "checkbox":
        try:
            loc = page.get_by_label(label, exact=False)
            if loc.count() > 0 and loc.first.is_visible():
                if not loc.first.is_checked():
                    loc.first.check()
                _step(step_num, f"Checking checkbox: \"{label}\" done")
                return True, step_num + 1
        except Exception:
            pass
        _step(step_num, f"Could not locate checkbox \"{label}\"")
        return False, step_num + 1

    # ── Radio buttons ─────────────────────────────────────────────────────
    if field_type == "radio":
        filled, step_num = _fill_radio(page, field, value, step_num)
        if not filled and is_required:
            _step(step_num, f"FAILED: Required radio \"{label}\" not filled")
        return filled, step_num

    # ── Dropdown / select ─────────────────────────────────────────────────
    if field_type == "select":
        filled, step_num = _fill_dropdown(page, field, value, step_num)
        if not filled and is_required:
            _step(step_num, f"FAILED: Required dropdown \"{label}\" not filled")
        return filled, step_num

    # ── File upload ───────────────────────────────────────────────────────
    if field_type == "file":
        try:
            file_inputs = page.locator('input[type="file"]')
            if file_inputs.count() > 0:
                file_inputs.first.set_input_files(str(CV_PATH))
                _step(step_num, f"Uploading CV -> {CV_PATH.name} done")
                return True, step_num + 1
        except Exception as e:
            _step(step_num, f"File upload failed: {e}")
        return False, step_num + 1

    # ── Skip ALL optional fields — only fill required/mandatory fields ────
    if not is_required:
        _step(step_num, f"Skipping optional field \"{label}\"")
        return False, step_num + 1

    # ── Text / textarea / email / tel / url — 4-strategy cascade ──────────
    filled = False

    # Strategy 1: Label
    if label and not filled:
        try:
            loc = page.get_by_label(label, exact=False)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.fill(value)
                filled = True
        except Exception:
            if is_required:
                _step(step_num, f"Label selector failed for \"{label}\"")

    # Strategy 2: Placeholder
    placeholder = field.get("placeholder", "")
    if placeholder and not filled:
        try:
            loc = page.get_by_placeholder(placeholder, exact=False)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.fill(value)
                filled = True
        except Exception:
            if is_required:
                _step(step_num, f"Placeholder selector failed for \"{label}\"")

    # Strategy 3: Name / id / aria-label attributes
    if not filled:
        search_terms = list({t for t in [candidate_field, normalized, label.lower()] if t and t != "other"})
        for term in search_terms:
            if filled:
                break
            tag = "textarea" if field_type == "textarea" else "input"
            selectors = [
                f'{tag}[name*="{term}" i]',
                f'{tag}[id*="{term}" i]',
                f'{tag}[aria-label*="{term}" i]',
                f'{tag}[placeholder*="{term}" i]',
            ]
            for sel in selectors:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.fill(value)
                        filled = True
                        break
                except Exception:
                    continue

    # Strategy 4: Fallback by element type (required fields only)
    if not filled and is_required and value:
        _step(step_num, f"Attribute selectors failed for required field \"{label}\" -- trying fallback")

        if candidate_field == "cover_letter" or normalized == "cover_letter":
            # Cover letter: fill first empty visible textarea
            try:
                textareas = page.locator("textarea")
                for i in range(min(textareas.count(), 5)):
                    ta = textareas.nth(i)
                    if ta.is_visible() and not ta.input_value():
                        ta.fill(value)
                        filled = True
                        _step(step_num, "Cover letter filled via textarea fallback")
                        break
            except Exception as e:
                _step(step_num, f"Textarea fallback failed: {e}")

        elif field_type == "textarea":
            try:
                textareas = page.locator("textarea")
                for i in range(min(textareas.count(), 5)):
                    ta = textareas.nth(i)
                    if ta.is_visible() and not ta.input_value():
                        ta.fill(value)
                        filled = True
                        _step(step_num, f"Filled \"{label}\" via textarea fallback")
                        break
            except Exception as e:
                _step(step_num, f"Textarea fallback failed: {e}")

        elif field_type in ("text", "email", "tel", "url"):
            input_type = field_type if field_type != "text" else None
            # For tel/url, also try text inputs as a fallback since many forms
            # use type="text" for all inputs regardless of semantic type
            type_selectors = []
            if input_type:
                type_selectors.append(f'input[type="{input_type}"]')
            if field_type in ("tel", "url"):
                type_selectors.append('input[type="text"], input:not([type])')
            if not type_selectors:
                type_selectors = ['input[type="text"], input:not([type])']
            try:
                for sel in type_selectors:
                    loc = page.locator(sel)
                    for i in range(min(loc.count(), 10)):
                        el = loc.nth(i)
                        if el.is_visible() and not el.input_value():
                            el.fill(value)
                            filled = True
                            _step(step_num, f"Filled \"{label}\" via input type fallback")
                            break
                    if filled:
                        break
            except Exception as e:
                _step(step_num, f"Input fallback failed: {e}")

    # ── Smart default for required fields with no value ───────────────────
    if not filled and is_required and not value:
        # Last resort: fill with "N/A" for text fields
        if field_type in ("text", "email", "tel", "url"):
            default = "N/A"
        elif field_type == "textarea":
            default = _get_answers().get("about_me", "N/A")
        else:
            default = ""

        if default:
            _step(step_num, f"Using smart default \"{default[:30]}\" for required field \"{label}\"")
            try:
                loc = page.get_by_label(label, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.fill(default)
                    filled = True
            except Exception:
                pass

    # ── Result logging ────────────────────────────────────────────────────
    if filled:
        display_val = value[:60] + "..." if len(value) > 60 else value
        _step(step_num, f"Filling \"{label}\" -> \"{display_val}\" done")
    elif is_required:
        _step(step_num, f"FAILED: Required field \"{label}\" could not be filled (all strategies exhausted)")
    else:
        _step(step_num, f"Could not locate field \"{label}\" -- skipping (optional)")

    return filled, step_num + 1


def _verify_required_fields(page: Page, fields: list[dict], step_num: int) -> tuple[bool, int, list[str]]:
    """Pre-submit safety check: verify all required fields are filled.

    Uses broad selectors matching the same strategies used during filling,
    including fallback checks for textareas and inputs by type.

    Returns (all_ok, step_num, list_of_empty_field_labels).
    """
    _step(step_num, "Pre-submit validation: checking required fields...")
    empty_required = []

    for field in fields:
        if not field.get("required"):
            continue

        label = field.get("label", "?")
        field_type = field.get("type", "text")
        candidate_field = field.get("candidate_field", "other")
        normalized = normalize_field_name(label)
        placeholder = field.get("placeholder", "")

        # Skip checkboxes (already handled) and file uploads
        if field_type in ("checkbox", "file"):
            continue

        # Build list of selectors to try (mirrors _fill_field strategies)
        tag = "textarea" if field_type == "textarea" else "input"
        search_terms = list({t for t in [candidate_field, normalized, label.lower()] if t and t != "other"})

        strategies = []
        # Strategy 1: label
        strategies.append(lambda lbl=label: page.get_by_label(lbl, exact=False))
        # Strategy 2: placeholder
        if placeholder:
            strategies.append(lambda ph=placeholder: page.get_by_placeholder(ph, exact=False))
        # Strategy 3: name/id/aria-label attributes
        for term in search_terms:
            strategies.append(lambda t=tag, s=term: page.locator(f'{t}[name*="{s}" i]'))
            strategies.append(lambda t=tag, s=term: page.locator(f'{t}[id*="{s}" i]'))
            strategies.append(lambda t=tag, s=term: page.locator(f'{t}[aria-label*="{s}" i]'))

        found_value = False
        for strategy in strategies:
            try:
                loc = strategy()
                if loc.count() > 0 and loc.first.is_visible():
                    val = loc.first.input_value()
                    if val and val.strip():
                        found_value = True
                        break
            except Exception:
                continue

        # Strategy 4: fallback — check ALL visible textareas/inputs of matching type
        if not found_value:
            try:
                if field_type == "textarea":
                    all_els = page.locator("textarea")
                elif field_type in ("email", "tel", "url"):
                    all_els = page.locator(f'input[type="{field_type}"]')
                else:
                    all_els = page.locator('input[type="text"], input:not([type])')

                for i in range(min(all_els.count(), 10)):
                    el = all_els.nth(i)
                    if el.is_visible():
                        val = el.input_value()
                        if val and val.strip():
                            found_value = True
                            break
            except Exception:
                pass

        if not found_value:
            empty_required.append(label)

    if empty_required:
        step_num += 1
        _step(step_num, f"VALIDATION FAILED: {len(empty_required)} required fields empty:")
        for lbl in empty_required:
            print(f"         - \"{lbl}\"")
        return False, step_num, empty_required
    else:
        step_num += 1
        _step(step_num, "All required fields filled -- ready to submit")
        return True, step_num, []


# ═══════════════════════════════════════════════════════════════════════════════
#  Button clicking and navigation
# ═══════════════════════════════════════════════════════════════════════════════

def _click_button(page: Page, button_text: str):
    """Try to click a button by its text using multiple strategies."""
    strategies = [
        lambda: page.get_by_role("button", name=button_text, exact=False),
        lambda: page.get_by_role("link", name=button_text, exact=False),
        lambda: page.get_by_text(button_text, exact=False),
        lambda: page.locator(f'button:has-text("{button_text}")'),
        lambda: page.locator(f'input[type="submit"][value*="{button_text}" i]'),
        lambda: page.locator(f'a:has-text("{button_text}")'),
        lambda: page.locator(f'[role="button"]:has-text("{button_text}")'),
    ]
    for fn in strategies:
        try:
            loc = fn()
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                return
        except Exception:
            continue
    raise Exception(f"Could not find button with text: \"{button_text}\"")


def _find_navigation_button(page: Page, texts: list[str]) -> str | None:
    """Check if any of the given button texts exist and are visible."""
    for text in texts:
        strategies = [
            lambda t=text: page.get_by_role("button", name=t, exact=False),
            lambda t=text: page.locator(f'button:has-text("{t}")'),
            lambda t=text: page.locator(f'input[type="submit"][value*="{t}" i]'),
            lambda t=text: page.locator(f'a:has-text("{t}")'),
        ]
        for fn in strategies:
            try:
                loc = fn()
                if loc.count() > 0 and loc.first.is_visible():
                    return text
            except Exception:
                continue
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def _ask_user_for_field_answer(field_label: str, company: str, title: str,
                               timeout_seconds: int = 300) -> str | None:
    """Send a WhatsApp message asking the user to fill an unknown form field.

    Pauses the calling thread, polling ConversationState every 5 seconds
    until the user replies (state == "field_answer_ready") or timeout expires.

    Returns the user's answer string, or None on timeout.
    """
    from core.notifier import send_whatsapp
    from db.database import get_session
    from db.models import ConversationState

    # Set state to pending_field so webhook knows to capture next reply
    session = get_session()
    try:
        row = session.query(ConversationState).first()
        if row:
            row.state = "pending_field"
            row.pending_field_label = field_label
            row.field_answer = None
            from datetime import timezone as tz
            row.updated_at = datetime.now(tz.utc)
            session.commit()
    finally:
        session.close()

    send_whatsapp(
        f"❓ *{company} — {title}*\n\n"
        f"נתקלתי בשאלה שאינה ב-default_answers:\n"
        f"*\"{field_label}\"*\n\n"
        f"שלח תשובה ואמשיך בהגשה. (timeout: 5 דקות)"
    )

    logger.info(f"Waiting for user answer to field: {field_label!r} (timeout={timeout_seconds}s)")
    elapsed = 0
    poll_interval = 5

    while elapsed < timeout_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval

        s = get_session()
        try:
            row = s.query(ConversationState).first()
            if row and row.state == "field_answer_ready" and row.field_answer:
                answer = row.field_answer
                # Reset state
                row.state = "idle"
                row.pending_field_label = None
                row.field_answer = None
                s.commit()
                logger.info(f"User answered field {field_label!r}: {answer[:80]}")
                return answer
        finally:
            s.close()

    # Timeout — reset state
    logger.warning(f"Timeout waiting for user answer to field: {field_label!r}")
    s2 = get_session()
    try:
        row = s2.query(ConversationState).first()
        if row and row.state == "pending_field":
            row.state = "idle"
            row.pending_field_label = None
            s2.commit()
    finally:
        s2.close()

    send_whatsapp(
        f"⏰ תם הזמן לשאלה: *\"{field_label}\"*\n"
        f"ממשיך עם תשובת ברירת מחדל."
    )
    return None


def apply_to_job(job_id: str, apply_url: str, job_title: str, company: str,
                 job_description: str, auto_submit: bool = False,
                 user_instruction: str = "",
                 cv_variant: str | None = None) -> dict:
    """Apply to a job by filling out the application form.

    user_instruction: optional guidance from the user injected into cover letter
    cv_variant: optional CV file variant name (e.g. "CV-Backend")
    Returns dict with keys: success, screenshots, cover_letter, error
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    ats_key = _extract_ats_key(apply_url)
    cv_path = _resolve_cv_path(cv_variant)
    result = {
        "success": False,
        "screenshots": [],
        "cover_letter": None,
        "error": None,
        "application_result": None,
    }

    step = 1
    _step(step, f"Opening job page: {apply_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=300)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        # Load LinkedIn session cookies if navigating to a LinkedIn URL
        if "linkedin.com" in apply_url:
            session_file = Path(__file__).parent.parent / "data" / "linkedin_session.json"
            if session_file.exists():
                cookies = json.loads(session_file.read_text())
                context.add_cookies(cookies)
                logger.info("LinkedIn session cookies loaded into applicator browser")

        page = context.new_page()

        try:
            # ── PHASE 1: Load page ──────────────────────────────────────
            # LinkedIn never reaches networkidle (continuous XHRs), use load instead
            wait_event = "load" if "linkedin.com" in apply_url else "networkidle"
            page.goto(apply_url, wait_until=wait_event, timeout=60000)
            page.wait_for_timeout(3000)

            step += 1
            shot = _screenshot(page, job_id, "01_initial_page")
            result["screenshots"].append(str(shot))
            _step(step, f"Screenshot saved -> {shot.name}")

            # ── PHASE 2: Dismiss popups ──────────────────────────────────
            step += 1
            _step(step, "Checking for popups or cookie banners...")
            _dismiss_popups(page)

            # ── PHASE 3: Detect & click Apply button if needed ───────────
            step += 1
            # LinkedIn job listing pages always have an Apply button to click,
            # even though they contain inputs (search bar) — never skip Apply search on LinkedIn
            on_linkedin_job_page = "linkedin.com/jobs/view/" in page.url
            form_already_visible = (not on_linkedin_job_page) and _has_visible_form(page)

            if form_already_visible:
                _step(step, "Form fields already visible -- skipping Apply button search")
            else:
                clicked, step = _find_and_click_apply_button_on_page(
                    page, client, job_id, step, result)

                if clicked:
                    step += 1
                    step = _wait_for_form(page, step)
                    step += 1
                    shot = _screenshot(page, job_id, "02_after_apply_click")
                    result["screenshots"].append(str(shot))
                    _step(step, f"Screenshot after Apply click -> {shot.name}")

                    # ── LinkedIn Easy Apply: modal opens in the same tab ──
                    if on_linkedin_job_page and len(context.pages) == 1:
                        modal_visible = False
                        for sel in EASY_APPLY_MODAL_SELECTORS:
                            try:
                                if page.locator(sel).count() > 0:
                                    modal_visible = True
                                    break
                            except Exception:
                                pass
                        if modal_visible:
                            answers_dict = yaml.safe_load(ANSWERS_PATH.read_text(encoding="utf-8")) or {}
                            cover_letter = _generate_cover_letter(
                                client, job_title, company, job_description or "", user_instruction
                            )
                            result["cover_letter"] = cover_letter
                            success, step = _fill_linkedin_easy_apply_modal(
                                page, client, job_id, cover_letter,
                                answers_dict, result, step, auto_submit=auto_submit,
                            )
                            result["success"] = success
                            result["application_result"] = "easy_apply" if success else "failed"
                            browser.close()
                            return result

                    if len(context.pages) > 1:
                        step += 1
                        _step(step, "New tab detected -- switching to application tab")
                        page = context.pages[-1]
                        try:
                            page.wait_for_load_state("load", timeout=30000)
                        except Exception:
                            pass
                        page.wait_for_timeout(3000)

                        # Always try to click an Apply button on the new tab —
                        # external career pages show a job description first, then
                        # the actual form only appears after clicking "Apply Now".
                        step += 1
                        shot = _screenshot(page, job_id, "02b_new_tab_landing")
                        result["screenshots"].append(str(shot))
                        _step(step, f"New tab loaded: {page.url[:80]} -- searching for Apply button...")
                        clicked2, step = _find_and_click_apply_button_on_page(
                            page, client, job_id, step, result)
                        if clicked2:
                            step = _wait_for_form(page, step + 1)
                            page.wait_for_timeout(2000)
                            # If another new tab opened, switch to it
                            if len(context.pages) > 2:
                                step += 1
                                _step(step, "Third tab detected -- switching...")
                                page = context.pages[-1]
                                try:
                                    page.wait_for_load_state("load", timeout=30000)
                                except Exception:
                                    pass
                                page.wait_for_timeout(2000)
                else:
                    if not _has_visible_form(page):
                        step += 1
                        shot = _screenshot(page, job_id, "02_no_form_found")
                        result["screenshots"].append(str(shot))
                        _step(step, "No form visible -- asking Claude Vision...")
                        guidance = _ask_claude_vision_for_page_state(client, shot)
                        _step(step + 1, f"Claude says: {guidance.get('message', 'unknown')}")
                        step += 1
                        if guidance.get("status") == "has_button":
                            btn = guidance.get("button_text", "Apply")
                            _step(step, f"Claude found button: \"{btn}\" -- clicking")
                            try:
                                _click_button(page, btn)
                                page.wait_for_timeout(2000)
                                step = _wait_for_form(page, step + 1)
                            except Exception as e:
                                _step(step, f"Could not click \"{btn}\": {e}")

            # ── PHASE 4: Generate cover letter ───────────────────────────
            step += 1
            _step(step, "Generating cover letter with Claude...")
            cover_letter = _generate_cover_letter(client, job_title, company, job_description or "", user_instruction)
            result["cover_letter"] = cover_letter
            step += 1
            _step(step, f"Cover letter generated:\n          \"{cover_letter}\"")

            # ── PHASE 4b: Detect & navigate into form iframe ───────────
            # Many ATS systems (Comeet, Greenhouse, Lever, etc.) embed the
            # application form in an iframe.  page.locator() cannot reach
            # inside iframes, so we navigate directly to the iframe src URL
            # which makes the form the main page content.
            step += 1
            iframe_keywords = ["comeet", "greenhouse", "lever", "ashby",
                               "workday", "smartrecruiters", "breezy",
                               "applytojob", "boards.eu"]
            iframes = page.locator("iframe")
            iframe_navigated = False
            for idx in range(iframes.count()):
                try:
                    src = iframes.nth(idx).get_attribute("src") or ""
                    if any(kw in src.lower() for kw in iframe_keywords):
                        _step(step, f"Form iframe detected: {src[:100]} -- navigating into it")
                        page.goto(src, wait_until="load", timeout=30000)
                        page.wait_for_timeout(2000)
                        iframe_navigated = True
                        break
                except Exception:
                    continue
            if not iframe_navigated:
                _step(step, "No ATS iframe found -- form is on the main page")

            # ── PHASE 5: Multi-page form loop ────────────────────────────
            page_num = 0
            max_pages = 10

            while page_num < max_pages:
                page_num += 1
                step += 1
                _step(step, f"--- Form page {page_num} ---")

                shot = _screenshot(page, job_id, f"03_page{page_num}_before_fill")
                result["screenshots"].append(str(shot))

                # Identify fields — try ATS cache first, fall back to Claude Vision
                step += 1
                cached = _get_cached_fields(ats_key) if ats_key and page_num == 1 else None
                if cached:
                    _step(step, f"Using cached ATS mapping for {ats_key}")
                    form_analysis = cached
                else:
                    _step(step, "Sending screenshot to Claude Vision for field detection...")
                    form_analysis = _identify_fields(client, shot)
                fields = form_analysis.get("fields", [])
                has_next = form_analysis.get("next_button", False)
                has_submit = form_analysis.get("submit_button", False)
                next_text = form_analysis.get("next_button_text", "Next")
                submit_text = form_analysis.get("submit_button_text", "Submit")

                step += 1
                _step(step, f"Claude identified {len(fields)} fields:")
                for f in fields:
                    req = " (required)" if f.get("required") else ""
                    norm = normalize_field_name(f.get("label", ""))
                    print(f"         - \"{f.get('label', '?')}\" ({f.get('type', '?')}) "
                          f"-> normalized: {norm}{req}")

                if not fields and not has_submit and not has_next:
                    step += 1
                    _step(step, "No fields detected -- checking DOM...")
                    if not _has_visible_form(page):
                        _step(step + 1, "No form in DOM. Page may require login.")
                        result["error"] = "No form fields found on page"
                        break
                    else:
                        _step(step + 1, "DOM has fields -- retrying Vision...")
                        page.evaluate("window.scrollTo(0, 0)")
                        page.wait_for_timeout(500)
                        shot = _screenshot(page, job_id, f"03_page{page_num}_retry")
                        result["screenshots"].append(str(shot))
                        form_analysis = _identify_fields(client, shot)
                        fields = form_analysis.get("fields", [])
                        has_next = form_analysis.get("next_button", False)
                        has_submit = form_analysis.get("submit_button", False)
                        next_text = form_analysis.get("next_button_text", "Next")
                        submit_text = form_analysis.get("submit_button_text", "Submit")

                # ── Fill each field ──────────────────────────────────────
                step += 1
                _step(step, "Filling form fields...")
                for field in fields:
                    candidate_field_key = field.get("candidate_field", "other")
                    field_label = field.get("label", "")

                    # Resolve value via answer database
                    if candidate_field_key == "cv_upload":
                        value = str(CV_PATH)
                    else:
                        value = lookup_answer(field_label, candidate_field_key,
                                              field.get("type", "text"))

                    # Cover letter override
                    if candidate_field_key == "cover_letter" or normalize_field_name(field_label) == "cover_letter":
                        value = cover_letter

                    _filled, step = _fill_field(page, field, value, step,
                                                cover_letter=cover_letter)

                    # WhatsApp fallback: required textarea with no value found
                    if not _filled and field.get("required") and \
                            field.get("type") == "textarea" and not value:
                        step += 1
                        _step(step, f"Required textarea \"{field_label}\" has no answer — asking user via WhatsApp")
                        user_answer = _ask_user_for_field_answer(
                            field_label, company, job_title)
                        if user_answer:
                            _step(step, f"User provided answer: \"{user_answer[:60]}\"")
                            _filled, step = _fill_field(
                                page, field, user_answer, step, cover_letter=cover_letter)
                        else:
                            _step(step, f"No answer received — leaving field empty")

                    page.wait_for_timeout(300)

                # ── Consent checkboxes ────────────────────────────────────
                step += 1
                _step(step, "Checking for consent/privacy checkboxes...")
                step = _check_consent_checkboxes(page, step)

                # Screenshot after filling
                step += 1
                shot = _screenshot(page, job_id, f"04_page{page_num}_after_fill")
                result["screenshots"].append(str(shot))
                _step(step, f"Screenshot after filling -> {shot.name}")

                # ── DOM-based navigation detection ────────────────────────
                if not has_next:
                    dom_next = _find_navigation_button(page, NEXT_BUTTON_TEXTS)
                    if dom_next:
                        has_next = True
                        next_text = dom_next

                if not has_submit:
                    dom_submit = _find_navigation_button(page, SUBMIT_BUTTON_TEXTS)
                    if dom_submit:
                        has_submit = True
                        submit_text = dom_submit

                # ── Navigate ──────────────────────────────────────────────
                if has_next and not (has_submit and page_num > 1):
                    step += 1
                    _step(step, f"Clicking \"{next_text}\" (page {page_num})...")
                    try:
                        _click_button(page, next_text)
                        page.wait_for_timeout(2000)
                        step = _wait_for_form(page, step + 1, timeout_ms=5000)
                    except Exception as e:
                        _step(step, f"Next click failed: {e}")
                        if has_submit:
                            pass
                        else:
                            result["error"] = f"Navigation failed: {e}"
                            break
                    else:
                        continue

                if has_submit:
                    # ── Pre-submit validation ─────────────────────────────
                    step += 1
                    all_ok, step, empty = _verify_required_fields(page, fields, step)

                    if not all_ok:
                        _step(step, f"Cannot submit: {len(empty)} required fields empty")
                        result["error"] = f"Required fields empty: {', '.join(empty)}"
                        shot = _screenshot(page, job_id, "05_validation_failed")
                        result["screenshots"].append(str(shot))
                        break

                    step += 1
                    shot = _screenshot(page, job_id, "05_before_submit")
                    result["screenshots"].append(str(shot))
                    _step(step, f"Screenshot before submit -> {shot.name}")

                    if auto_submit:
                        step += 1
                        _step(step, "AUTO-SUBMIT mode -- submitting now...")
                    else:
                        step += 1
                        _step(step, "READY TO SUBMIT -- waiting for confirmation...")
                        print("          Press ENTER to submit or CTRL+C to cancel: ",
                              end="", flush=True)
                        try:
                            input()
                        except KeyboardInterrupt:
                            print("\n          Cancelled by user.")
                            result["error"] = "Cancelled by user"
                            return result

                    step += 1
                    _step(step, f"Submit button clicked: \"{submit_text}\"")
                    try:
                        _click_button(page, submit_text)
                        page.wait_for_timeout(3000)

                        step += 1
                        _step(step, "Checking submission result...")

                        # Check for URL redirect (confirmation page)
                        current_url = page.url.lower()
                        url_success = any(kw in current_url for kw in [
                            "thank", "success", "confirm", "complete",
                            "submitted", "received", "done",
                        ])
                        if url_success:
                            step += 1
                            _step(step, f"Redirect to confirmation URL detected: {page.url}")

                        # Strategy 1: Text-based success detection
                        success_phrases = [
                            "application submitted",
                            "thank you for applying",
                            "your application has been received",
                            "application complete",
                            "thanks for applying",
                            "successfully submitted",
                            "we have received your application",
                            "application sent",
                            "thank you for your interest",
                            "we'll be in touch",
                            "application has been submitted",
                        ]
                        page_text = ""
                        try:
                            page_text = page.inner_text("body").lower()
                        except Exception:
                            pass

                        text_success = any(phrase in page_text for phrase in success_phrases)
                        if text_success:
                            step += 1
                            _step(step, "Success message detected on page")

                        # Strategy 2: Check for validation errors
                        error_phrases = [
                            "please fill", "required field", "is required",
                            "please correct", "there were errors",
                            "fix the following", "invalid",
                        ]
                        has_errors = any(phrase in page_text for phrase in error_phrases)

                        if has_errors and not text_success:
                            step += 1
                            _step(step, "Validation errors detected on page after submit")
                            shot = _screenshot(page, job_id, "06_validation_error")
                            result["screenshots"].append(str(shot))
                            result["error"] = "Post-submit validation errors detected"
                            result["application_result"] = "failed"
                            break

                        # Take success/post-submit screenshot
                        step += 1
                        if text_success or url_success:
                            shot = _screenshot(page, job_id, "submission_success")
                        else:
                            shot = _screenshot(page, job_id, "06_after_submit")
                        result["screenshots"].append(str(shot))
                        _step(step, f"Screenshot after submit -> {shot.name}")

                        # If text/URL already confirmed success, skip Vision
                        if text_success or url_success:
                            step += 1
                            _step(step, "Application submitted successfully!")
                            result["success"] = True
                            result["application_result"] = "success"
                        else:
                            # Strategy 3: Claude Vision fallback
                            step += 1
                            _step(step, "No clear success text -- verifying with Claude Vision...")
                            post = _ask_claude_vision_for_page_state(client, shot)
                            vis_status = post.get("status", "unknown")
                            msg = post.get("message", "")

                            if vis_status == "success":
                                step += 1
                                _step(step, f"Claude confirms success: {msg}")
                                result["success"] = True
                                result["application_result"] = "success"
                                # Rename screenshot to success
                                success_shot = _screenshot(page, job_id, "submission_success")
                                result["screenshots"].append(str(success_shot))
                            elif vis_status == "error":
                                step += 1
                                _step(step, f"Claude detected error: {msg}")
                                result["error"] = f"Post-submit error: {msg}"
                                result["application_result"] = "failed"
                            else:
                                step += 1
                                _step(step, f"Page state unclear ({vis_status}). Assuming success.")
                                result["success"] = True
                                result["application_result"] = "success"

                    except Exception as e:
                        _step(step, f"Submit failed: {e}")
                        result["error"] = f"Submit failed: {e}"
                        result["application_result"] = "failed"

                    break

                else:
                    step += 1
                    _step(step, "No Next/Submit button. Analyzing page...")
                    shot = _screenshot(page, job_id, f"04_page{page_num}_stuck")
                    result["screenshots"].append(str(shot))

                    guidance = _ask_claude_vision_for_page_state(client, shot)
                    status = guidance.get("status", "unknown")

                    if status == "success":
                        _step(step + 1, "Page indicates successful submission!")
                        result["success"] = True
                        result["application_result"] = "success"
                        _screenshot(page, job_id, "submission_success")
                        break
                    elif status == "has_button":
                        btn = guidance.get("button_text", "Submit")
                        step += 1
                        _step(step, f"Claude found button: \"{btn}\" -- clicking...")
                        try:
                            _click_button(page, btn)
                            page.wait_for_timeout(2000)
                            continue
                        except Exception as e:
                            _step(step, f"Could not click \"{btn}\": {e}")
                    else:
                        _step(step + 1, f"Unknown page state ({status}). Stopping.")
                        result["error"] = "Could not find Next/Submit button"
                        break

        except Exception as e:
            logger.error(f"Application failed for {job_id}: {e}")
            step += 1
            _step(step, f"Error: {e}")
            try:
                shot = _screenshot(page, job_id, "error")
                result["screenshots"].append(str(shot))
            except Exception:
                pass
            result["error"] = str(e)

        finally:
            browser.close()

    # Save ATS field mappings on success for future reuse
    if result["success"] and ats_key and form_analysis:
        try:
            _save_ats_fields(ats_key, form_analysis)
        except Exception as e:
            logger.warning(f"Failed to save ATS memory for {ats_key}: {e}")

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers that need the Claude client
# ═══════════════════════════════════════════════════════════════════════════════

def _find_and_click_apply_button_on_page(
    page: Page, client: anthropic.Anthropic, job_id: str, step: int, result: dict
) -> tuple[bool, int]:
    """Search for Apply button via DOM, then Claude Vision fallback."""
    _step(step, "Searching for Apply button...")

    for text in APPLY_BUTTON_TEXTS:
        strategies = [
            lambda t=text: page.get_by_role("button", name=t, exact=False),
            lambda t=text: page.get_by_role("link", name=t, exact=False),
            lambda t=text: page.locator(f'button:has-text("{t}")'),
            lambda t=text: page.locator(f'a:has-text("{t}")'),
            lambda t=text: page.locator(f'[role="button"]:has-text("{t}")'),
            lambda t=text: page.locator(f'input[type="submit"][value*="{t}" i]'),
        ]
        for fn in strategies:
            try:
                loc = fn()
                if loc.count() > 0 and loc.first.is_visible():
                    step += 1
                    _step(step, f"Apply button detected: \"{text}\" -> clicking")
                    loc.first.click()
                    page.wait_for_timeout(1500)
                    return True, step
            except Exception:
                continue

    # Claude Vision fallback
    step += 1
    _step(step, "No standard Apply button found. Asking Claude Vision...")
    shot = _screenshot(page, job_id, "02_apply_btn_search")
    result["screenshots"].append(str(shot))
    b64 = _image_to_base64(shot)

    try:
        raw = _ask_claude_vision(
            client, b64,
            "I'm looking at a job posting page. I need to find the Apply button. "
            "Is there an Apply / Apply Now / Submit Application button visible? "
            "Respond with JSON: {\"found\": true/false, \"button_text\": \"...\", \"description\": \"...\"}"
        )
        info = _parse_json_response(raw)
        if info.get("found"):
            btn_text = info.get("button_text", "Apply")
            step += 1
            _step(step, f"Claude found Apply button: \"{btn_text}\" -> clicking")
            _click_button(page, btn_text)
            page.wait_for_timeout(1500)
            return True, step
    except Exception as e:
        logger.debug(f"Claude Vision apply button detection failed: {e}")

    step += 1
    _step(step, "No Apply button found -- assuming form is already visible")
    return False, step


def _ask_claude_vision_for_page_state(client: anthropic.Anthropic, screenshot_path: Path) -> dict:
    """Ask Claude Vision to analyze the current page state."""
    b64 = _image_to_base64(screenshot_path)
    try:
        raw = _ask_claude_vision(
            client, b64,
            "What is the current state of this page? Is this:\n"
            "- A success/confirmation page (application submitted)?\n"
            "- An error page?\n"
            "- A page with a button to proceed (what text)?\n"
            "- A login page?\n"
            "- Something else?\n\n"
            "Respond with JSON: {\"status\": \"success|error|has_button|login|unknown\", "
            "\"button_text\": \"...\", \"message\": \"brief description\"}"
        )
        return _parse_json_response(raw)
    except Exception as e:
        logger.debug(f"Page state analysis failed: {e}")
        return {"status": "unknown", "message": str(e)}


# ── LinkedIn Easy Apply ───────────────────────────────────────────────────────

EASY_APPLY_MODAL_SELECTORS = [
    "div.jobs-easy-apply-modal",
    "div[data-test-modal-id='easy-apply-modal']",
    "div[class*='easy-apply-modal']",
    "div[aria-labelledby*='easy-apply']",
]

EASY_APPLY_BUTTON_SELECTORS = [
    "button.jobs-apply-button",
    "button[class*='jobs-apply-button']",
    'button:has-text("Easy Apply")',
    'button[aria-label*="Easy Apply"]',
]

EASY_APPLY_NEXT_SELECTORS = [
    'button[aria-label="Continue to next step"]',
    'button[aria-label="Submit application"]',
    'footer button.artdeco-button--primary',
    'div.jobs-easy-apply-modal button.artdeco-button--primary',
]

EASY_APPLY_SUCCESS_SELECTORS = [
    'h3:has-text("Your application was sent")',
    'h3:has-text("Application submitted")',
    'div[class*="post-apply"]',
    '[data-test-job-seeker-application-outcome-confirmation]',
]

LINKEDIN_FIELD_MAP = {
    # The modal reuses LinkedIn profile data but shows editable inputs
    "phone country code": "phone",
    "mobile phone number": "phone",
    "email address": "email",
    "first name": "first_name",
    "last name": "last_name",
    "city": "city",
    "linkedin profile url": "linkedin",
    "website": "website",
    "github": "github",
    "years of experience": "years_of_experience",
    "how many years": "years_of_experience",
    "cover letter": "cover_letter",
    "summary": "summary",
    "salary": "salary_expectation",
}


def _fill_linkedin_easy_apply_modal(
    page: Page,
    client: anthropic.Anthropic,
    job_id: str,
    cover_letter: str,
    answers: dict,
    result: dict,
    step: int,
    auto_submit: bool = False,
) -> tuple[bool, int]:
    """Fill and submit a LinkedIn Easy Apply modal.

    Steps through the multi-page modal using Next/Submit buttons.
    Reuses existing _fill_field strategies for standard inputs.
    Returns (success: bool, updated_step: int).
    """
    step += 1
    _step(step, "LinkedIn Easy Apply modal detected — starting modal fill...")

    max_modal_pages = 8
    modal_page = 0

    while modal_page < max_modal_pages:
        modal_page += 1
        step += 1
        _step(step, f"--- Easy Apply modal step {modal_page} ---")

        page.wait_for_timeout(1500)

        shot = _screenshot(page, job_id, f"ea_{modal_page:02d}_before")
        result["screenshots"].append(str(shot))

        # ── 1. Handle resume/CV upload ────────────────────────────────────
        try:
            file_inputs = page.locator(
                'input[type="file"][name*="resume"], '
                'input[type="file"][accept*="pdf"], '
                'input[type="file"]'
            )
            if file_inputs.count() > 0 and CV_PATH.exists():
                fi = file_inputs.first
                if fi.is_visible() or True:  # file inputs are often hidden
                    fi.set_input_files(str(CV_PATH))
                    step += 1
                    _step(step, f"Resume uploaded: {CV_PATH.name}")
                    page.wait_for_timeout(1000)
        except Exception as e:
            logger.debug(f"Resume upload attempt: {e}")

        # ── 2. Ask Claude Vision for field list ───────────────────────────
        step += 1
        _step(step, "Sending modal screenshot to Claude Vision for fields...")
        form_analysis = _identify_fields(client, shot)
        fields = form_analysis.get("fields", [])
        _step(step, f"Claude found {len(fields)} fields in modal step {modal_page}")

        # ── 3. Fill each detected field ───────────────────────────────────
        for field in fields:
            label = field.get("label", "")
            # Override candidate_field mapping for LinkedIn-specific labels
            norm_lower = label.lower().strip()
            for li_label, canonical in LINKEDIN_FIELD_MAP.items():
                if li_label in norm_lower:
                    field["candidate_field"] = canonical
                    break

            value = answers.get(field.get("candidate_field", ""), "")
            _, step = _fill_field(page, field, value, step, cover_letter=cover_letter)

        # ── 4. Consent / privacy checkboxes ──────────────────────────────
        step = _check_consent_checkboxes(page, step)

        # ── 5. Take post-fill screenshot ──────────────────────────────────
        shot_after = _screenshot(page, job_id, f"ea_{modal_page:02d}_after")
        result["screenshots"].append(str(shot_after))

        # ── 6. Check for success page ─────────────────────────────────────
        for sel in EASY_APPLY_SUCCESS_SELECTORS:
            try:
                if page.locator(sel).count() > 0:
                    step += 1
                    _step(step, "Easy Apply submitted successfully!")
                    return True, step
            except Exception:
                pass

        # ── 7. Detect Next vs Submit button ───────────────────────────────
        has_next = form_analysis.get("next_button", False)
        has_submit = form_analysis.get("submit_button", False)
        next_text = form_analysis.get("next_button_text", "Next")
        submit_text = form_analysis.get("submit_button_text", "Submit application")

        # Also try LinkedIn-specific button selectors
        primary_btn = page.locator(
            'footer button.artdeco-button--primary, '
            'div.jobs-easy-apply-modal button.artdeco-button--primary'
        )

        if has_submit or "review" in (next_text or "").lower():
            step += 1
            if not auto_submit:
                _step(step, f"[DRY RUN] Would click Submit: \"{submit_text}\"")
                result["application_result"] = "dry_run"
                return True, step
            _step(step, f"Clicking Submit: \"{submit_text}\"")
            try:
                _click_button(page, submit_text)
            except Exception:
                if primary_btn.count() > 0:
                    primary_btn.last.click()
            page.wait_for_timeout(3000)
            shot_final = _screenshot(page, job_id, "ea_final_submit")
            result["screenshots"].append(str(shot_final))
            # Verify success
            for sel in EASY_APPLY_SUCCESS_SELECTORS:
                try:
                    if page.locator(sel).count() > 0:
                        step += 1
                        _step(step, "Application submitted — success confirmed")
                        return True, step
                except Exception:
                    pass
            # Claude Vision final check
            state = _ask_claude_vision_for_page_state(client, shot_final)
            if state.get("status") == "success":
                _step(step, "Claude Vision confirmed: application submitted")
                return True, step
            _step(step, f"Submit clicked but outcome unclear: {state.get('message', '?')}")
            return True, step  # optimistic — form was submitted

        elif has_next:
            step += 1
            _step(step, f"Clicking Next: \"{next_text}\"")
            try:
                _click_button(page, next_text)
            except Exception:
                if primary_btn.count() > 0:
                    primary_btn.first.click()
            page.wait_for_timeout(1500)
        else:
            # Try the primary button as a last resort
            if primary_btn.count() > 0:
                btn_text = primary_btn.first.inner_text().strip()
                step += 1
                _step(step, f"Clicking primary button: \"{btn_text}\"")
                primary_btn.first.click()
                page.wait_for_timeout(1500)
                if "submit" in btn_text.lower() or "send" in btn_text.lower():
                    return True, step
            else:
                step += 1
                _step(step, "No Next/Submit button found — modal may be complete")
                break

    logger.warning("Easy Apply modal: reached max steps without confirmed submission")
    return False, step
