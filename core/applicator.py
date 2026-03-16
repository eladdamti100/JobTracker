"""Auto form-filling and job application via Playwright + Groq Vision.

Architecture:
- Central answer database: data/default_answers.yaml
- Field name normalization: maps any label to a canonical key
- 4-strategy fill cascade for required fields
- Dropdown / radio / checkbox handling
- Pre-submit validation: refuse to submit if required fields empty
- Multi-page form loop with Next/Submit detection
- Groq Vision for field identification and page state analysis

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

from openai import OpenAI
import yaml
from loguru import logger
from playwright.sync_api import sync_playwright, Page

from core.content_generator import ContentGenerator

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
    "given name(s) - latin script": "first_name", "given names - latin script": "first_name",
    "last name": "last_name", "surname": "last_name", "family name": "last_name",
    "family name - latin script": "last_name",

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
                  field_type: str = "text",
                  content_generator: "ContentGenerator | None" = None,
                  options: list[str] | None = None) -> str:
    """Look up the answer for a field from the central answer database.

    Priority:
    1. candidate_field from Groq Vision (if it maps to an answer)
    2. Normalized field label
    3. AI-generated answer via ContentGenerator (for any unrecognized field)
    4. Smart defaults based on field type
    """
    answers = _get_answers()

    # 1. Try candidate_field directly (from Groq Vision analysis)
    if candidate_field and candidate_field in answers:
        return str(answers[candidate_field])

    # 2. Normalize the label and look up
    normalized = normalize_field_name(field_label)
    if normalized in answers:
        return str(answers[normalized])

    # 3. AI fallback: ask Groq to determine the value for this unknown field
    if content_generator is not None:
        generated = content_generator.generate(
            field_label=field_label,
            field_type=field_type,
            options=options or [],
        )
        if generated:
            return generated

    # 4. Smart defaults based on field type (last resort)
    if field_type == "textarea":
        return str(answers.get("about_me", ""))
    if field_type == "select":
        return ""  # Will be handled by dropdown logic
    if field_type == "radio":
        return ""  # Will be handled by radio logic

    return ""


# ═══════════════════════════════════════════════════════════════════════════════
#  DOM Option Extractors  (JS parsing via page.evaluate)
# ═══════════════════════════════════════════════════════════════════════════════

# Placeholder values that should be excluded from extracted option lists
_PLACEHOLDER_OPTION_TEXTS = {
    "", "select", "select...", "select one", "select an option",
    "choose", "choose...", "please select", "--", "---", "none",
}


def _extract_native_select_options(page: "Page", label: str) -> list[str]:
    """Extract option texts from a native <select> element matching the label.

    Uses JS to read all <option> values directly from the DOM — faster and
    more reliable than Playwright's .all_text_contents() for large option lists.

    Returns a list of option texts (excluding placeholders), or [] if not found.
    """
    normalized = normalize_field_name(label)
    strategies = [
        lambda: page.get_by_label(label, exact=False),
        lambda: page.locator(f'select[name*="{normalized}" i]'),
        lambda: page.locator(f'select[id*="{normalized}" i]'),
        lambda: page.locator(f'select[aria-label*="{label}" i]'),
    ]
    select_el = None
    for fn in strategies:
        try:
            loc = fn()
            if loc.count() > 0 and loc.first.is_visible():
                tag = loc.first.evaluate("el => el.tagName")
                if tag == "SELECT":
                    select_el = loc.first
                    break
        except Exception:
            continue

    if not select_el:
        return []

    try:
        # Read all option texts in one JS call — avoids N round-trips
        texts: list[str] = select_el.evaluate(
            "el => Array.from(el.options).map(o => o.text.trim())"
        )
        return [t for t in texts if t.lower() not in _PLACEHOLDER_OPTION_TEXTS]
    except Exception:
        return []


def _extract_combobox_options(page: "Page", label: str) -> list[str]:
    """Extract options from a custom combobox/React dropdown.

    Works for Workday, Greenhouse, and other SPAs that render dropdowns as
    [role="combobox"] + [role="option"] or [data-automation-id="promptOption"].

    Strategy:
    1. Find the combobox trigger by label, aria-label, or nearby text
    2. Click to open it
    3. Read visible [role="option"] / promptOption items via JS
    4. Press Escape to close without selecting

    Returns a list of option texts, or [] if not found / already open.
    """
    normalized = normalize_field_name(label)
    label_lower = label.lower()

    # ── Find the combobox trigger ─────────────────────────────────────────
    trigger = None
    trigger_strategies = [
        lambda: page.locator(f'[role="combobox"][aria-label*="{label}" i]'),
        lambda: page.locator(f'[role="combobox"][id*="{normalized}" i]'),
        lambda: page.get_by_label(label, exact=False).filter(
            has=page.locator('[role="combobox"]')),
        # Workday pattern: find combobox whose nearby label text matches
        lambda: _find_combobox_near_label(page, label_lower),
    ]
    for fn in trigger_strategies:
        try:
            loc = fn()
            if loc is not None and loc.count() > 0 and loc.first.is_visible():
                trigger = loc.first
                break
        except Exception:
            continue

    if not trigger:
        return []

    try:
        # Open the dropdown
        trigger.scroll_into_view_if_needed()
        trigger.click()
        page.wait_for_timeout(600)

        # Read options via JS in one round-trip
        options: list[str] = page.evaluate("""() => {
            const selectors = [
                '[data-automation-id="promptOption"]',
                '[role="option"]',
                '[role="listitem"]',
                'li[class*="option" i]',
            ];
            for (const sel of selectors) {
                const els = Array.from(document.querySelectorAll(sel))
                    .filter(el => el.offsetParent !== null);  // visible only
                if (els.length > 0)
                    return els.map(el => el.textContent.trim());
            }
            return [];
        }""")

        # Close dropdown without selecting
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        return [t for t in options if t.lower() not in _PLACEHOLDER_OPTION_TEXTS]
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return []


def _find_combobox_near_label(page: "Page", label_lower: str):
    """Find a combobox whose nearest visible label text contains the hint.

    Iterates all [role="combobox"] elements and checks if their associated
    label (via aria-labelledby, for=, or parent text) matches label_lower.
    Returns a Locator for the first match, or None.
    """
    try:
        combos = page.locator('[role="combobox"]')
        count = combos.count()
        for i in range(min(count, 20)):
            combo = combos.nth(i)
            if not combo.is_visible():
                continue
            # Check aria-labelledby
            labelledby = combo.get_attribute("aria-labelledby") or ""
            if labelledby:
                try:
                    lbl_text = page.locator(f'#{labelledby}').first.inner_text().lower()
                    if label_lower in lbl_text or lbl_text in label_lower:
                        return combos.nth(i)
                except Exception:
                    pass
            # Check parent container text
            try:
                parent_text = combo.locator("xpath=../..").inner_text().lower()
                if label_lower in parent_text:
                    return combos.nth(i)
            except Exception:
                pass
    except Exception:
        pass
    return None


def _pick_best_option(
    options: list[str],
    value: str,
    field_label: str,
    field_type: str,
    content_generator: "ContentGenerator | None",
) -> str | None:
    """Choose the best option from a list for the given field value.

    Strategy:
    1. Exact match (case-insensitive)
    2. Substring match — value inside option or option inside value
    3. ContentGenerator — ask Groq to pick from the list
    4. First option (fallback)

    Returns the exact option text to use, or None if options is empty.
    """
    if not options:
        return None

    value_lower = (value or "").lower().strip()

    # 1. Exact match
    for opt in options:
        if opt.lower() == value_lower:
            return opt

    # 2. Substring match
    if value_lower:
        for opt in options:
            if value_lower in opt.lower() or opt.lower() in value_lower:
                return opt

    # 3. Groq picks from the list
    if content_generator is not None:
        generated = content_generator.generate(
            field_label=field_label,
            field_type=field_type,
            options=options,
        )
        if generated:
            # Validate it's actually one of the options
            gen_lower = generated.lower().strip()
            for opt in options:
                if opt.lower() == gen_lower or gen_lower in opt.lower():
                    return opt
            # If Groq returned something not in the list, log and fall through
            logger.debug(
                f"_pick_best_option: Groq returned {generated!r} not in options, "
                f"falling back to first"
            )

    # 4. First option
    return options[0]


# ═══════════════════════════════════════════════════════════════════════════════
#  Utilities
# ═══════════════════════════════════════════════════════════════════════════════

def _step(step_num: int, msg: str):
    """Print a formatted step message."""
    print(f"[STEP {step_num}] {msg}")
    logger.info(f"[STEP {step_num}] {msg}")


def _screenshot(page: Page, job_id: str, name: str) -> Path:
    """Take a screenshot and save it. Returns path even if screenshot fails."""
    shot_dir = SCREENSHOTS_DIR / job_id
    shot_dir.mkdir(parents=True, exist_ok=True)
    path = shot_dir / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True, timeout=8_000)
    except Exception:
        try:
            # Fallback: viewport only (no scroll — much faster)
            page.screenshot(path=str(path), full_page=False, timeout=5_000)
        except Exception:
            pass  # Screenshot failed silently — don't crash the flow
    return path


def _image_to_base64(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _parse_json_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON from Groq response."""
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
#  Groq Vision helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ask_grok_vision(client: OpenAI, screenshot_b64: str, prompt: str) -> str:
    """Send a screenshot to Groq Vision and get a text response."""
    response = client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}",
                    },
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }],
    )
    return (response.choices[0].message.content or "").strip()


def _identify_fields(client: OpenAI, screenshot_path: Path) -> dict:
    """Use Groq Vision to identify form fields from a screenshot."""
    if not screenshot_path.exists():
        logger.debug(f"Screenshot missing for Vision, skipping: {screenshot_path}")
        return {}
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

    raw = _ask_grok_vision(client, b64, prompt)
    return _parse_json_response(raw)


def _generate_cover_letter(client: OpenAI, job_title: str, company: str,
                           job_description: str, user_instruction: str = "") -> str:
    """Generate a short, tailored cover letter using Groq.

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
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


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


def _fill_dropdown(page: Page, field: dict, value: str, step_num: int,
                   content_generator: "ContentGenerator | None" = None) -> tuple[bool, int]:
    """Handle select/dropdown fields — both native <select> and custom comboboxes.

    Strategy:
    1. Native <select>: extract options via JS, pick best match with _pick_best_option
    2. Custom combobox ([role="combobox"]): open → extract visible options via JS →
       pick best with _pick_best_option → click it
    3. Fallback: first non-placeholder option

    Returns (filled, step_num).
    """
    label = field.get("label", "")
    filled = False

    # ── Path A: Native <select> ───────────────────────────────────────────────
    select_loc = None
    normalized = normalize_field_name(label)
    sel_strategies = [
        lambda: page.get_by_label(label, exact=False),
        lambda: page.locator(f'select[name*="{normalized}" i]'),
        lambda: page.locator(f'select[id*="{normalized}" i]'),
        lambda: page.locator(f'select[aria-label*="{label}" i]'),
    ]
    for fn in sel_strategies:
        try:
            loc = fn()
            if loc.count() > 0 and loc.first.is_visible():
                if loc.first.evaluate("el => el.tagName") == "SELECT":
                    select_loc = loc.first
                    break
        except Exception:
            continue

    if not select_loc:
        try:
            selects = page.locator("select")
            for i in range(min(selects.count(), 10)):
                sel = selects.nth(i)
                if sel.is_visible():
                    try:
                        sel_id = sel.get_attribute("id") or ""
                        sel_name = sel.get_attribute("name") or ""
                        if (normalized in sel_id.lower() or normalized in sel_name.lower()):
                            select_loc = sel
                            break
                    except Exception:
                        pass
        except Exception:
            pass

    if select_loc:
        try:
            options = _extract_native_select_options(page, label)
            if not options:
                # Fallback read
                options = [t for t in select_loc.locator("option").all_text_contents()
                           if t.strip().lower() not in _PLACEHOLDER_OPTION_TEXTS]

            best = _pick_best_option(options, value, label, "select", content_generator)
            if best:
                try:
                    select_loc.select_option(label=best)
                except Exception:
                    select_loc.select_option(value=best)
                _step(step_num, f"Dropdown \"{label}\" -> \"{best}\" done")
                filled = True
        except Exception as e:
            _step(step_num, f"Native select \"{label}\" failed: {e}")
        return filled, step_num + 1

    # ── Path B: Custom combobox (React/Workday/SPA pattern) ──────────────────
    options = _extract_combobox_options(page, label)
    if options:
        best = _pick_best_option(options, value, label, "select", content_generator)
        if best:
            # Re-open and click the chosen option
            try:
                trigger = _find_combobox_near_label(page, label.lower())
                if trigger is None:
                    # Try generic combobox locators
                    for sel in (
                        f'[role="combobox"][aria-label*="{label}" i]',
                        f'[role="combobox"][id*="{normalized}" i]',
                    ):
                        loc = page.locator(sel)
                        if loc.count() > 0 and loc.first.is_visible():
                            trigger = loc.first
                            break

                if trigger:
                    trigger.scroll_into_view_if_needed()
                    trigger.click()
                    page.wait_for_timeout(600)

                    # Click the matching option
                    for opt_sel in (
                        '[data-automation-id="promptOption"]',
                        '[role="option"]',
                        '[role="listitem"]',
                    ):
                        opts = page.locator(opt_sel)
                        for i in range(min(opts.count(), 50)):
                            try:
                                opt = opts.nth(i)
                                if not opt.is_visible():
                                    continue
                                t = (opt.inner_text() or "").strip()
                                if t.lower() == best.lower() or best.lower() in t.lower():
                                    opt.click()
                                    page.wait_for_timeout(400)
                                    _step(step_num, f"Combobox \"{label}\" -> \"{t}\" done")
                                    filled = True
                                    break
                            except Exception:
                                continue
                        if filled:
                            break

                    if not filled:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(200)
            except Exception as e:
                _step(step_num, f"Combobox \"{label}\" click failed: {e}")
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass

    if not filled:
        _step(step_num, f"Dropdown/combobox \"{label}\" — no matching element found")

    return filled, step_num + 1


def _fill_radio(page: Page, field: dict, value: str, step_num: int,
                content_generator: "ContentGenerator | None" = None) -> tuple[bool, int]:
    """Handle radio button groups (yes/no questions, multiple choice).

    Strategy:
    1. Collect all visible radio options from the DOM (label text + value attr)
    2. Use _pick_best_option (with ContentGenerator) to choose the best match
    3. Check the matching radio

    Returns (filled, step_num).
    """
    label = field.get("label", "")
    normalized = normalize_field_name(label)
    answers = _get_answers()

    # Determine the answer
    answer = value or answers.get(normalized, "")
    if not answer and normalized in YES_NO_KEYS:
        answer = answers.get(normalized, "Yes")

    filled = False

    try:
        radios = page.locator('input[type="radio"]')
        count = radios.count()

        # ── Collect all radio options from the DOM ────────────────────────
        radio_options: list[tuple[str, str]] = []  # (label_text, value_attr)
        for i in range(min(count, 30)):
            radio = radios.nth(i)
            if not radio.is_visible():
                continue
            radio_label = ""
            try:
                radio_id = radio.get_attribute("id")
                if radio_id:
                    lbl = page.locator(f'label[for="{radio_id}"]')
                    if lbl.count() > 0:
                        radio_label = lbl.first.inner_text().strip()
            except Exception:
                pass
            if not radio_label:
                try:
                    radio_label = radio.locator("xpath=..").inner_text().strip()
                except Exception:
                    pass
            radio_val = radio.get_attribute("value") or ""
            radio_options.append((radio_label, radio_val))

        # ── Pick the best option ──────────────────────────────────────────
        option_texts = [lbl for lbl, _ in radio_options if lbl]
        if not option_texts:
            option_texts = [val for _, val in radio_options if val]

        if answer:
            best = _pick_best_option(
                option_texts, answer, label, "radio", content_generator
            )
        elif content_generator and option_texts:
            # No static answer — let Groq decide from the available options
            best = content_generator.generate(
                field_label=label, field_type="radio", options=option_texts
            )
        else:
            best = None

        if not best and option_texts:
            best = option_texts[0]  # last resort: first option

        # ── Check the matching radio ──────────────────────────────────────
        if best:
            best_lower = best.lower().strip()
            for i, (rl, rv) in enumerate(radio_options):
                if (best_lower in rl.lower() or rl.lower() in best_lower or
                        best_lower == rv.lower()):
                    radios.nth(i).check()
                    _step(step_num, f"Radio \"{label}\" -> \"{rl or rv}\" done")
                    filled = True
                    break

    except Exception as e:
        _step(step_num, f"Radio \"{label}\" failed: {e}")

    return filled, step_num + 1


def _fill_field(page: Page, field: dict, value: str, step_num: int,
                cover_letter: str = "",
                content_generator: "ContentGenerator | None" = None) -> tuple[bool, int]:
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
        value = lookup_answer(label, candidate_field, field_type,
                              content_generator=content_generator,
                              options=field.get("options"))

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
        filled, step_num = _fill_radio(page, field, value, step_num,
                                       content_generator=content_generator)
        if not filled and is_required:
            _step(step_num, f"FAILED: Required radio \"{label}\" not filled")
        return filled, step_num

    # ── Dropdown / select ─────────────────────────────────────────────────
    if field_type == "select":
        filled, step_num = _fill_dropdown(page, field, value, step_num,
                                          content_generator=content_generator)
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

    # ── ContentGenerator retry for required fields that failed ───────────
    # Triggered when: field is required, still not filled, and either:
    #   a) The YAML value didn't work (value mismatch on dropdowns/combos), OR
    #   b) The YAML had no answer (value was empty)
    if not filled and is_required and content_generator is not None:
        if field_type in ("select", "radio"):
            # For dropdowns/radio: extract options and let Groq pick
            opts = (field.get("options") or
                    _extract_native_select_options(page, label) or
                    _extract_combobox_options(page, label))
            if opts:
                _step(step_num, f"AI retry: picking from {len(opts)} options for \"{label}\"")
                # Re-run the appropriate fill with a Groq-generated value
                ai_value = _pick_best_option(opts, value, label, field_type, content_generator)
                if ai_value:
                    if field_type == "select":
                        filled, step_num = _fill_dropdown(
                            page, field, ai_value, step_num, content_generator=None
                        )
                    else:
                        filled, step_num = _fill_radio(
                            page, field, ai_value, step_num, content_generator=None
                        )
        elif field_type in ("text", "email", "tel", "url", "textarea"):
            ai_value = content_generator.generate(
                field_label=label, field_type=field_type,
                options=field.get("options") or [],
            )
            if ai_value:
                _step(step_num, f"AI retry: filling \"{label}\" -> \"{ai_value[:60]}\"")
                try:
                    loc = page.get_by_label(label, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.fill(ai_value)
                        filled = True
                        value = ai_value
                except Exception:
                    pass

    # ── Smart default for required fields still not filled ────────────────
    if not filled and is_required and not value:
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
#  Login / Signup / 2FA handling
# ═══════════════════════════════════════════════════════════════════════════════

def _vision_agent_pass(
    page: "Page",
    client: OpenAI,
    content_generator: "ContentGenerator | None",
    job_id: str,
    step: int,
    max_retries: int = 2,
) -> int:
    """Vision agent: screenshot the current form, ask Groq Vision what's still
    unfilled or wrong, execute the suggested actions, then verify.

    This is the last-resort layer — runs after all other fill strategies have
    been attempted. Groq Vision sees exactly what a human would see and returns
    a JSON list of actions to take.

    Action format returned by Vision:
        [
          {
            "field_label": "Degree",
            "action": "select" | "fill" | "click" | "check",
            "value": "Bachelor of Science",
            "selector_hint": "optional CSS or aria hint"
          }, ...
        ]

    Args:
        max_retries: how many screenshot→act→verify cycles to run
    Returns updated step number.
    """
    logger.info(f"[{job_id}] Vision agent pass starting (max_retries={max_retries})")

    # Build candidate context summary for the prompt
    try:
        from core.content_generator import _get_candidate_data, _build_projects_block, _build_skills_block
        data = _get_candidate_data()
        general_ctx = (data.get("general_context") or "").strip().replace("\n", " ")
        projects_summary = _build_projects_block(data)
        skills_summary = _build_skills_block(data)
    except Exception:
        general_ctx = "Software Engineering student, Bar Ilan University, GPA 89+"
        projects_summary = ""
        skills_summary = ""

    answers = _get_answers()
    candidate_summary = (
        f"Name: {answers.get('full_name', '')}, "
        f"Email: {answers.get('email', '')}, "
        f"Phone: {answers.get('phone', '')}, "
        f"University: {answers.get('university', '')}, "
        f"GPA: {answers.get('gpa', '')}, "
        f"Degree: Bachelor of Science in Software Engineering, "
        f"Year: {answers.get('current_year_of_study', '3')}, "
        f"Skills: {answers.get('skills', '')}. "
        f"{general_ctx}"
    )

    vision_prompt = f"""You are helping fill out a job application form.

CANDIDATE PROFILE:
{candidate_summary}

PROJECTS:
{projects_summary}

SKILLS:
{skills_summary}

Look at this screenshot of the current form page.
Identify ALL fields that are:
  1. Visually empty or showing a placeholder / "Select One"
  2. Required (marked with * or "required")

For each such field, return the action needed to fill it.

Respond with a JSON array ONLY — no markdown, no explanation:
[
  {{
    "field_label": "<exact label text visible on the form>",
    "action": "fill" | "select" | "click" | "check",
    "value": "<the value to enter or option to select>",
    "selector_hint": "<optional: CSS selector or aria-label hint if visible in DOM>"
  }}
]

Rules:
- "fill": for text inputs and textareas
- "select": for dropdowns, comboboxes, radio buttons
- "check": for checkboxes
- "click": for buttons that need to be clicked (e.g. upload, add)
- Use candidate data above to determine values
- For "Degree": use "Bachelor of Science" or "Bachelor's"
- For Yes/No questions about work authorization: answer "Yes"
- For visa sponsorship: answer "No"
- If the form looks complete and ready to submit, return []
"""

    for attempt in range(max_retries):
        # Take screenshot
        shot_path = _screenshot(page, job_id, f"vision_agent_{attempt:02d}")
        if not shot_path.exists():
            logger.warning(f"[{job_id}] Vision agent: screenshot failed, skipping")
            break

        # Ask Groq Vision
        try:
            b64 = _image_to_base64(shot_path)
            response = client.chat.completions.create(
                model="llama-3.2-90b-vision-preview",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": vision_prompt},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/png;base64,{b64}"
                            }},
                        ],
                    }
                ],
                max_tokens=1024,
                temperature=0.1,
            )
            raw = response.choices[0].message.content or ""
            actions = _parse_json_response(raw)
            if not isinstance(actions, list):
                logger.warning(f"[{job_id}] Vision agent: unexpected response format")
                break
        except Exception as e:
            logger.error(f"[{job_id}] Vision agent: Vision API call failed: {e}")
            break

        if not actions:
            logger.info(f"[{job_id}] Vision agent: no unfilled fields detected")
            break

        logger.info(f"[{job_id}] Vision agent attempt {attempt + 1}: "
                    f"{len(actions)} action(s) to execute")

        # Execute each action
        any_filled = False
        for action_item in actions:
            field_label = action_item.get("field_label", "")
            action = action_item.get("action", "fill")
            value = action_item.get("value", "")
            selector_hint = action_item.get("selector_hint", "")

            step += 1
            _step(step, f"Vision agent: {action} \"{field_label}\" -> \"{value[:50]}\"")

            try:
                if action == "fill":
                    # Try label → selector_hint → aria-label
                    filled = False
                    for loc_fn in [
                        lambda: page.get_by_label(field_label, exact=False),
                        lambda: page.locator(selector_hint) if selector_hint else None,
                    ]:
                        try:
                            loc = loc_fn()
                            if loc is None:
                                continue
                            if loc.count() > 0 and loc.first.is_visible():
                                loc.first.fill(value)
                                filled = True
                                any_filled = True
                                break
                        except Exception:
                            continue
                    if not filled:
                        _step(step, f"Vision agent: could not locate field \"{field_label}\"")

                elif action == "select":
                    # Try native select first, then combobox
                    pseudo_field = {"label": field_label, "type": "select", "options": []}
                    filled, step = _fill_dropdown(page, pseudo_field, value, step,
                                                  content_generator=content_generator)
                    if not filled:
                        pseudo_field["type"] = "radio"
                        filled, step = _fill_radio(page, pseudo_field, value, step,
                                                   content_generator=content_generator)
                    if filled:
                        any_filled = True

                elif action == "check":
                    try:
                        loc = page.get_by_label(field_label, exact=False)
                        if loc.count() > 0 and loc.first.is_visible():
                            if not loc.first.is_checked():
                                loc.first.check()
                            any_filled = True
                    except Exception:
                        pass

                elif action == "click":
                    try:
                        page.get_by_text(value, exact=False).first.click()
                        page.wait_for_timeout(500)
                        any_filled = True
                    except Exception:
                        pass

            except Exception as e:
                _step(step, f"Vision agent: action failed for \"{field_label}\": {e}")

        if not any_filled:
            logger.info(f"[{job_id}] Vision agent: no actions succeeded, stopping")
            break

        page.wait_for_timeout(800)

    return step


def _auto_verify_email(platform_key: str, page, client: OpenAI,
                       job_id: str, step: int, result: dict) -> tuple[bool, int]:
    """Check inbox for verification email. Handles both link and code types.

    - Link: clicks it via HTTP request
    - Code: enters it into the code field on the current page

    Returns (verified, step).
    """
    from core.email_verifier import auto_verify

    step += 1
    _step(step, f"Checking inbox for {platform_key} verification email (up to 2 min)...")

    verify_result = auto_verify(platform_key, max_wait=120)
    if not verify_result:
        step += 1
        _step(step, "Could not find verification email — notifying user")
        from core.notifier import send_whatsapp
        send_whatsapp(
            f"📧 Could not auto-verify email for *{platform_key}*.\n"
            f"Please check your inbox and verify manually."
        )
        result["error"] = f"Email verification needed for {platform_key}"
        return False, step

    if verify_result["type"] == "link":
        # Link was already clicked by auto_verify via HTTP GET.
        # Also navigate the browser to the link — Amazon requires the same session
        # to complete the verification flow and redirect to the app.
        link_url = verify_result["value"]
        step += 1
        _step(step, f"Email verified via link for {platform_key} — navigating browser to verify URL")
        try:
            page.goto(link_url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(2_000)
        except Exception:
            page.wait_for_timeout(3_000)
        return True, step

    # Code type — need to get code input page first, then fetch fresh code.
    # Strategy: click resend/enter-code link on the page FIRST so a NEW email
    # is dispatched, THEN poll IMAP for the fresh code (old codes may be invalid).
    step += 1
    _step(step, "Clicking resend/verify link to trigger fresh verification code...")

    resend_texts = [
        "Click here to resend", "resend the verification", "Resend",
        "Enter verification code", "Enter code", "verify your account",
    ]
    for text in resend_texts:
        try:
            link = page.locator(f'a:has-text("{text}"), button:has-text("{text}")').first
            if link.is_visible():
                link.click()
                page.wait_for_timeout(3000)
                step += 1
                _step(step, "Clicked resend/verify link — waiting for fresh code in inbox...")
                break
        except Exception:
            continue

    # Re-fetch from IMAP for the freshest code (avoids using an expired/old code)
    from core.email_verifier import find_verification_email as _find_email
    fresh = _find_email(platform_key, max_wait=90, poll_interval=10)
    if fresh and fresh["type"] == "code":
        code = fresh["value"]
        step += 1
        _step(step, f"Got fresh verification code: {code}")
    else:
        # Fall back to the code we already found if no new one arrived
        code = verify_result["value"]
        step += 1
        _step(step, f"Using existing verification code: {code}")

    # Now look for the code input field
    code_selectors = [
        'input[name*="code"]', 'input[name*="otp"]', 'input[name*="verification"]',
        'input[autocomplete="one-time-code"]',
        'input[type="text"][maxlength="6"]', 'input[type="number"][maxlength="6"]',
        'input[type="text"]',
    ]
    filled = False
    for sel in code_selectors:
        try:
            field = page.locator(sel).first
            if field.is_visible():
                field.fill(code)
                filled = True
                break
        except Exception:
            continue

    if not filled:
        step += 1
        _step(step, "Code input not found — notifying user")
        from core.notifier import send_whatsapp
        send_whatsapp(
            f"📧 Please enter verification code *{code}* for *{platform_key}* manually."
        )
        result["error"] = "Could not enter verification code on page"
        return False, step

    # Submit the code
    for btn_text in ["Verify", "Submit", "Continue", "Log in", "Sign in", "Next"]:
        try:
            btn = page.get_by_role("button", name=btn_text)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                break
        except Exception:
            continue

    page.wait_for_timeout(3000)
    shot = _screenshot(page, job_id, "verify_code_result")
    result.setdefault("screenshots", []).append(str(shot))

    step += 1
    _step(step, f"Verification code {code} submitted for {platform_key}")
    return True, step


def _handle_login_page(page, client: OpenAI, job_id: str, apply_url: str,
                       step: int, result: dict) -> tuple[bool, int]:
    """Handle a login page: try stored credentials, or sign up.

    Returns (logged_in: bool, step: int).
    """
    from core.credential_manager import (
        resolve_platform_key, get_credential, save_credential,
        PLATFORMS_NO_AUTO_SIGNUP,
    )

    platform_key = resolve_platform_key(page.url)
    step += 1
    _step(step, f"Platform detected: {platform_key}")

    # Try stored credentials first
    cred = get_credential(platform_key)
    if cred:
        email, password = cred
        step += 1
        _step(step, f"Found stored credentials for {platform_key}, logging in...")
        success, step = _perform_login(page, client, email, password,
                                       platform_key, job_id, step, result)
        if success:
            return True, step
        # Login failed — credentials might be wrong, try signup
        _step(step, "Stored credentials failed, will try signup...")

    # No credentials or login failed — try signup
    if platform_key in PLATFORMS_NO_AUTO_SIGNUP:
        step += 1
        _step(step, f"Auto-signup disabled for {platform_key} — asking user...")
        from core.notifier import send_whatsapp
        send_whatsapp(
            f"🔐 *{platform_key.title()}* requires login.\n"
            f"Auto-signup is disabled for this platform.\n"
            f"Please log in manually or provide credentials."
        )
        result["error"] = f"Login required for {platform_key} (no auto-signup)"
        return False, step

    # Attempt signup
    success, step = _handle_signup_page(page, client, job_id, apply_url,
                                        platform_key, step, result)
    return success, step


def _perform_login(page, client: OpenAI, email: str, password: str,
                   platform_key: str, job_id: str, step: int,
                   result: dict) -> tuple[bool, int]:
    """Fill login form fields and submit. Returns (success, step)."""
    from core.credential_manager import (
        PLATFORM_SELECTORS, LOGIN_BUTTON_TEXTS, mark_login_success,
    )

    # Try platform-specific selectors first, then generic
    selectors = PLATFORM_SELECTORS.get(platform_key, {})
    email_sel = selectors.get("email", 'input[type="email"], input[name*="email"], '
                              'input[name*="user"], input[autocomplete="username"], '
                              'input[autocomplete="email"]')
    pass_sel = selectors.get("password", 'input[type="password"], '
                             'input[name*="pass"], input[autocomplete="current-password"]')

    # Fill email
    try:
        email_field = page.locator(email_sel).first
        if email_field.is_visible():
            email_field.fill(email)
            step += 1
            _step(step, f"Filled email: {email}")
    except Exception as e:
        logger.warning(f"Could not fill email field: {e}")

    # Fill password
    try:
        pass_field = page.locator(pass_sel).first
        if pass_field.is_visible():
            pass_field.fill(password)
            step += 1
            _step(step, "Filled password")
    except Exception as e:
        logger.warning(f"Could not fill password field: {e}")

    # Click login button
    signin_sel = selectors.get("signin")
    clicked = False
    if signin_sel:
        try:
            btn = page.locator(signin_sel).first
            if btn.is_visible():
                btn.click()
                clicked = True
        except Exception:
            pass

    if not clicked:
        for text in LOGIN_BUTTON_TEXTS:
            try:
                btn = page.get_by_role("button", name=text)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    clicked = True
                    break
            except Exception:
                continue

    if not clicked:
        # Fallback: try any submit button
        try:
            submit = page.locator('input[type="submit"], button[type="submit"]').first
            if submit.is_visible():
                submit.click()
                clicked = True
        except Exception:
            pass

    if not clicked:
        step += 1
        _step(step, "Could not find login button")
        return False, step

    step += 1
    _step(step, "Clicked login button, waiting for response...")

    # Wait for navigation
    page.wait_for_timeout(4000)
    shot = _screenshot(page, job_id, "login_result")
    result["screenshots"].append(str(shot))

    # Check if login succeeded
    page_state = _ask_grok_vision_for_page_state(client, shot)
    status = page_state.get("status", "unknown")

    if status == "login":
        # Still on login page — check if it's an email verification issue
        step += 1
        page_text = page.inner_text("body")[:500].lower()
        if any(kw in page_text for kw in ["hasn't been verified", "not verified",
                                           "verify your email", "check your email",
                                           "verification email", "confirm your email"]):
            _step(step, "Email not verified — auto-verifying via IMAP...")
            verified, step = _auto_verify_email(platform_key, page, client,
                                                 job_id, step, result)
            if verified:
                # Retry login after verification
                step += 1
                _step(step, "Email verified — retrying login...")
                page.reload()
                page.wait_for_timeout(2000)
                return _perform_login(page, client, email, password,
                                      platform_key, job_id, step, result)
        _step(step, "Login failed — still on login page")
        return False, step

    if status == "2fa":
        step += 1
        _step(step, "2FA required — asking user for code...")
        return _handle_2fa(page, client, job_id, platform_key, step, result)

    # Login succeeded
    step += 1
    _step(step, f"Login successful for {platform_key}")
    mark_login_success(platform_key)
    return True, step


def _handle_signup_page(page, client: OpenAI, job_id: str, apply_url: str,
                        platform_key: str, step: int,
                        result: dict) -> tuple[bool, int]:
    """Navigate to signup, create account, store credentials. Returns (success, step)."""
    from core.credential_manager import (
        SIGNUP_LINK_TEXTS, PLATFORM_SELECTORS, generate_secure_password,
        save_credential,
    )

    # Find and click signup link
    step += 1
    _step(step, "Looking for signup/register link...")

    selectors = PLATFORM_SELECTORS.get(platform_key, {})
    create_sel = selectors.get("create_account")
    clicked = False

    if create_sel:
        try:
            link = page.locator(create_sel).first
            if link.is_visible():
                link.click()
                clicked = True
        except Exception:
            pass

    if not clicked:
        for text in SIGNUP_LINK_TEXTS:
            try:
                link = page.get_by_role("link", name=text)
                if link.count() > 0 and link.first.is_visible():
                    link.first.click()
                    clicked = True
                    break
            except Exception:
                continue

    if not clicked:
        # Try broader text matching
        for text in SIGNUP_LINK_TEXTS:
            try:
                link = page.locator(f'a:has-text("{text}")').first
                if link.is_visible():
                    link.click()
                    clicked = True
                    break
            except Exception:
                continue

    if not clicked:
        step += 1
        _step(step, "Could not find signup link")
        result["error"] = "No signup link found on login page"
        return False, step

    # Wait for signup page
    page.wait_for_timeout(3000)
    step += 1
    _step(step, "Signup page loaded")

    shot = _screenshot(page, job_id, "signup_page")
    result["screenshots"].append(str(shot))

    # Get user email from answers database
    answers = _get_answers()
    user_email = answers.get("email", "")
    if not user_email:
        logger.error("No email in default_answers.yaml for signup")
        result["error"] = "No email configured for signup"
        return False, step

    # Generate secure password
    new_password = generate_secure_password()
    step += 1
    _step(step, f"Generated secure password for signup")

    # Fill signup form fields using DOM selectors
    # Email
    for sel in ['input[type="email"]', 'input[name*="email"]',
                'input[autocomplete="email"]', 'input[id*="email"]']:
        try:
            field = page.locator(sel).first
            if field.is_visible():
                field.fill(user_email)
                _step(step, f"Filled signup email: {user_email}")
                break
        except Exception:
            continue

    # Password
    pass_fields = page.locator('input[type="password"]')
    pass_count = pass_fields.count()
    if pass_count >= 1:
        try:
            pass_fields.nth(0).fill(new_password)
            step += 1
            _step(step, "Filled password")
        except Exception as e:
            logger.warning(f"Could not fill password: {e}")

    # Confirm password (second password field)
    if pass_count >= 2:
        try:
            pass_fields.nth(1).fill(new_password)
            step += 1
            _step(step, "Filled confirm password")
        except Exception as e:
            logger.warning(f"Could not fill confirm password: {e}")

    # Fill name fields if present
    first_name = answers.get("first_name", "")
    last_name = answers.get("last_name", "")
    for sel, val in [
        ('input[name*="first"], input[id*="first"]', first_name),
        ('input[name*="last"], input[id*="last"]', last_name),
    ]:
        if val:
            try:
                field = page.locator(sel).first
                if field.is_visible():
                    field.fill(val)
            except Exception:
                pass

    # Check consent/terms checkboxes
    _check_consent_checkboxes(page, step)

    # Click signup button
    signup_clicked = False
    for text in ["Sign Up", "Create Account", "Register", "Create",
                 "Submit", "Next", "Continue"]:
        try:
            btn = page.get_by_role("button", name=text)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                signup_clicked = True
                break
        except Exception:
            continue

    if not signup_clicked:
        try:
            submit = page.locator('input[type="submit"], button[type="submit"]').first
            if submit.is_visible():
                submit.click()
                signup_clicked = True
        except Exception:
            pass

    if not signup_clicked:
        step += 1
        _step(step, "Could not find signup button")
        result["error"] = "No signup button found"
        return False, step

    # Wait and check result
    page.wait_for_timeout(4000)
    shot = _screenshot(page, job_id, "signup_result")
    result["screenshots"].append(str(shot))

    page_state = _ask_grok_vision_for_page_state(client, shot)
    status = page_state.get("status", "unknown")

    if status == "login":
        # Redirected back to login — signup might have worked, try logging in
        step += 1
        _step(step, "Redirected to login after signup — trying to log in...")
        save_credential(platform_key, user_email, new_password)

        # Check if email verification is needed (common after signup)
        page_text = page.inner_text("body")[:500].lower()
        if any(kw in page_text for kw in ["hasn't been verified", "not verified",
                                           "verify your email", "check your email",
                                           "verification email", "confirm your email"]):
            step += 1
            _step(step, "Email verification required — checking inbox...")
            verified, step = _auto_verify_email(platform_key, page, client,
                                                 job_id, step, result)
            if not verified:
                return False, step

        return _perform_login(page, client, user_email, new_password,
                              platform_key, job_id, step, result)

    if status in ("error", "unknown"):
        msg = page_state.get("message", "Unknown signup error")
        # Check if "already exists" type error
        page_text = page.inner_text("body")[:500].lower()
        if any(kw in page_text for kw in ["already exists", "already registered",
                                           "account exists", "already have"]):
            step += 1
            _step(step, "Account already exists — asking user for credentials via WhatsApp")
            from core.notifier import send_whatsapp
            send_whatsapp(
                f"🔐 Account already exists on *{platform_key}*.\n"
                f"Email: {user_email}\n"
                f"Please reply with your password, or log in manually."
            )
            result["error"] = f"Account already exists on {platform_key}"
            return False, step
        step += 1
        _step(step, f"Signup error: {msg}")
        result["error"] = f"Signup failed: {msg}"
        return False, step

    # Signup succeeded
    step += 1
    _step(step, f"Signup successful for {platform_key}")
    save_credential(platform_key, user_email, new_password)
    return True, step


def _handle_2fa(page, client: OpenAI, job_id: str, platform_key: str,
                step: int, result: dict) -> tuple[bool, int]:
    """Ask user for 2FA code via WhatsApp, enter it, and submit."""
    from core.notifier import send_whatsapp
    from db.database import get_session
    from db.models import ConversationState
    import time

    # Ask user via WhatsApp
    send_whatsapp(
        f"🔐 *2FA code needed* for {platform_key}.\n"
        f"Please reply with the verification code."
    )

    # Set conversation state to pending_field
    session = get_session()
    try:
        row = session.query(ConversationState).first()
        if row:
            row.state = "pending_field"
            row.pending_field_label = "2FA verification code"
            row.field_answer = None
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
    finally:
        session.close()

    # Poll for answer (5 min timeout)
    timeout = 300
    start = time.time()
    code = None
    while time.time() - start < timeout:
        time.sleep(5)
        s = get_session()
        try:
            row = s.query(ConversationState).first()
            if row and row.state == "field_answer_ready" and row.field_answer:
                code = row.field_answer.strip()
                row.state = "idle"
                row.field_answer = None
                s.commit()
                break
        finally:
            s.close()

    if not code:
        step += 1
        _step(step, "2FA timeout — no code received")
        result["error"] = "2FA code not provided within timeout"
        return False, step

    # Fill code into the page
    step += 1
    _step(step, f"Entering 2FA code: {code[:2]}****")

    # Try common 2FA input selectors
    for sel in ['input[name*="code"]', 'input[name*="otp"]', 'input[name*="mfa"]',
                'input[name*="token"]', 'input[name*="verify"]',
                'input[type="text"]', 'input[type="number"]']:
        try:
            field = page.locator(sel).first
            if field.is_visible():
                field.fill(code)
                break
        except Exception:
            continue

    # Click submit/verify
    for text in ["Verify", "Submit", "Continue", "Confirm"]:
        try:
            btn = page.get_by_role("button", name=text)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                break
        except Exception:
            continue

    page.wait_for_timeout(3000)
    shot = _screenshot(page, job_id, "2fa_result")
    result["screenshots"].append(str(shot))

    page_state = _ask_grok_vision_for_page_state(client, shot)
    if page_state.get("status") in ("2fa", "login"):
        step += 1
        _step(step, "2FA verification failed")
        return False, step

    step += 1
    _step(step, "2FA verified successfully")
    from core.credential_manager import mark_login_success
    mark_login_success(platform_key)
    return True, step


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
    client = OpenAI(
        api_key=os.environ["GROQ_API_KEY"],
        base_url="https://api.groq.com/openai/v1",
    )
    content_gen = ContentGenerator(
        client=client,
        job_title=job_title,
        company=company,
        job_description=job_description or "",
    )
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

            # ── PHASE 2b: Check if landing page is a login/signup page ──
            initial_state = _ask_grok_vision_for_page_state(client, shot)
            initial_status = initial_state.get("status", "unknown")

            if initial_status in ("login", "signup"):
                step += 1
                _step(step, f"Landing page is a {initial_status} page — handling auth...")
                logged_in, step = _handle_login_page(
                    page, client, job_id, apply_url, step, result)
                if not logged_in:
                    result["error"] = result.get("error") or "Login/signup failed"
                    browser.close()
                    return result
                # Navigate back to the job page after login
                step += 1
                _step(step, f"Navigating back to job page: {apply_url}")
                page.goto(apply_url, wait_until="load", timeout=60000)
                page.wait_for_timeout(3000)
                shot = _screenshot(page, job_id, "01b_after_login")
                result["screenshots"].append(str(shot))
            elif initial_status == "2fa":
                step += 1
                _step(step, "Landing page requires 2FA...")
                from core.credential_manager import resolve_platform_key
                platform_key = resolve_platform_key(page.url)
                success_2fa, step = _handle_2fa(
                    page, client, job_id, platform_key, step, result)
                if not success_2fa:
                    result["error"] = "2FA verification failed"
                    browser.close()
                    return result
                page.goto(apply_url, wait_until="load", timeout=60000)
                page.wait_for_timeout(3000)
            elif initial_status == "captcha":
                step += 1
                _step(step, "CAPTCHA detected — cannot proceed automatically")
                result["error"] = "CAPTCHA detected on landing page"
                browser.close()
                return result

            # ── PHASE 3: Detect & click Apply button if needed ───────────
            step += 1
            # LinkedIn job listing pages always have an Apply button to click,
            # even though they contain inputs (search bar) — never skip Apply search on LinkedIn
            on_linkedin_job_page = "linkedin.com/jobs/view/" in page.url
            # If PHASE 2b Vision found an Apply/button, trust Vision over DOM form check
            # (avoids false positives from search bars being detected as form fields)
            vision_says_has_button = (initial_status == "has_button")
            form_already_visible = (
                (not on_linkedin_job_page) and
                (not vision_says_has_button) and
                _has_visible_form(page)
            )

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

                    # ── Check if Apply button led to a login page ──
                    post_apply_state = _ask_grok_vision_for_page_state(client, shot)
                    post_apply_status = post_apply_state.get("status", "unknown")
                    if post_apply_status in ("login", "signup"):
                        step += 1
                        _step(step, f"Apply button led to {post_apply_status} page — handling auth...")
                        logged_in, step = _handle_login_page(
                            page, client, job_id, apply_url, step, result)
                        if logged_in:
                            step += 1
                            _step(step, "Logged in — navigating back to job page to re-apply...")
                            page.goto(apply_url, wait_until="load", timeout=60000)
                            page.wait_for_timeout(3000)
                            # Re-click Apply now that we're logged in
                            clicked2, step = _find_and_click_apply_button_on_page(
                                page, client, job_id, step + 1, result)
                            if clicked2:
                                step = _wait_for_form(page, step + 1)
                                page.wait_for_timeout(2000)
                                # Check if we landed on login AGAIN
                                recheck_shot = _screenshot(page, job_id, "02c_recheck")
                                result["screenshots"].append(str(recheck_shot))
                                recheck_state = _ask_grok_vision_for_page_state(client, recheck_shot)
                                if recheck_state.get("status") in ("login", "signup"):
                                    step += 1
                                    _step(step, "Still on login — using stored credentials...")
                                    from core.credential_manager import get_credential, resolve_platform_key
                                    pk = resolve_platform_key(page.url)
                                    cred = get_credential(pk)
                                    if cred:
                                        login_ok, step = _perform_login(
                                            page, client, cred[0], cred[1],
                                            pk, job_id, step, result)
                                        if login_ok:
                                            # After login, Amazon may redirect to the application form directly
                                            page.wait_for_timeout(3000)
                                    else:
                                        _step(step, "No credentials found — cannot proceed")
                                        result["error"] = "Login required but no credentials available"
                                        browser.close()
                                        return result
                        else:
                            result["error"] = result.get("error") or "Login failed after Apply click"
                            browser.close()
                            return result

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
                                content_generator=content_gen,
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
                        _step(step, "No form visible -- asking Groq Vision...")
                        guidance = _ask_grok_vision_for_page_state(client, shot)
                        _step(step + 1, f"Groq says: {guidance.get('message', 'unknown')}")
                        step += 1
                        guidance_status = guidance.get("status", "unknown")
                        if guidance_status == "has_button":
                            btn = guidance.get("button_text", "Apply")
                            _step(step, f"Groq found button: \"{btn}\" -- clicking")
                            try:
                                _click_button(page, btn)
                                page.wait_for_timeout(2000)
                                step = _wait_for_form(page, step + 1)
                            except Exception as e:
                                _step(step, f"Could not click \"{btn}\": {e}")
                        elif guidance_status in ("login", "signup"):
                            _step(step, f"Page requires {guidance_status} — handling auth...")
                            logged_in, step = _handle_login_page(
                                page, client, job_id, apply_url, step, result)
                            if logged_in:
                                step += 1
                                _step(step, "Logged in — navigating back to job page...")
                                page.goto(apply_url, wait_until="load", timeout=60000)
                                page.wait_for_timeout(3000)

            # ── PHASE 4: Generate cover letter ───────────────────────────
            step += 1
            _step(step, "Generating cover letter with Groq...")
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

                # Identify fields — try ATS cache first, fall back to Groq Vision
                step += 1
                cached = _get_cached_fields(ats_key) if ats_key and page_num == 1 else None
                if cached:
                    _step(step, f"Using cached ATS mapping for {ats_key}")
                    form_analysis = cached
                else:
                    _step(step, "Sending screenshot to Groq Vision for field detection...")
                    form_analysis = _identify_fields(client, shot)
                fields = form_analysis.get("fields", [])
                has_next = form_analysis.get("next_button", False)
                has_submit = form_analysis.get("submit_button", False)
                next_text = form_analysis.get("next_button_text", "Next")
                submit_text = form_analysis.get("submit_button_text", "Submit")

                step += 1
                _step(step, f"Groq identified {len(fields)} fields:")
                for f in fields:
                    req = " (required)" if f.get("required") else ""
                    norm = normalize_field_name(f.get("label", ""))
                    print(f"         - \"{f.get('label', '?')}\" ({f.get('type', '?')}) "
                          f"-> normalized: {norm}{req}")

                if not fields and not has_submit and not has_next:
                    step += 1
                    _step(step, "No fields detected -- checking DOM...")
                    if not _has_visible_form(page):
                        # Check if this is a login/signup page
                        step += 1
                        no_form_shot = _screenshot(page, job_id, f"03_page{page_num}_no_form")
                        result["screenshots"].append(str(no_form_shot))
                        no_form_state = _ask_grok_vision_for_page_state(client, no_form_shot)
                        no_form_status = no_form_state.get("status", "unknown")

                        if no_form_status in ("login", "signup"):
                            _step(step, f"Page is a {no_form_status} page — handling auth...")
                            logged_in, step = _handle_login_page(
                                page, client, job_id, apply_url, step, result)
                            if logged_in:
                                step += 1
                                _step(step, "Logged in — navigating back to job page...")
                                page.goto(apply_url, wait_until="load", timeout=60000)
                                page.wait_for_timeout(3000)
                                page_num -= 1  # retry this form page
                                continue
                            else:
                                result["error"] = result.get("error") or "Login/signup failed"
                                break
                        elif no_form_status == "2fa":
                            from core.credential_manager import resolve_platform_key
                            pk = resolve_platform_key(page.url)
                            success_2fa, step = _handle_2fa(
                                page, client, job_id, pk, step, result)
                            if success_2fa:
                                page.goto(apply_url, wait_until="load", timeout=60000)
                                page.wait_for_timeout(3000)
                                page_num -= 1
                                continue
                            else:
                                result["error"] = "2FA verification failed"
                                break
                        else:
                            _step(step, "No form in DOM. Page may require login.")
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
                                              field.get("type", "text"),
                                              content_generator=content_gen,
                                              options=field.get("options"))

                    # Cover letter override
                    if candidate_field_key == "cover_letter" or normalize_field_name(field_label) == "cover_letter":
                        value = cover_letter

                    _filled, step = _fill_field(page, field, value, step,
                                                cover_letter=cover_letter,
                                                content_generator=content_gen)

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
                        _step(step, f"{len(empty)} required fields empty — running Vision agent pass")
                        # Vision agent: last attempt to fix remaining empty fields
                        step = _vision_agent_pass(
                            page, client, content_gen, job_id, step, max_retries=2
                        )
                        # Re-validate after Vision agent
                        all_ok, step, empty = _verify_required_fields(page, fields, step)

                    if not all_ok:
                        _step(step, f"Cannot submit: {len(empty)} required fields still empty after Vision agent")
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
                            # Check if this is actually a login page, not a validation error
                            step += 1
                            post_shot = _screenshot(page, job_id, "06_post_submit_check")
                            result["screenshots"].append(str(post_shot))
                            post_state = _ask_grok_vision_for_page_state(client, post_shot)
                            post_status = post_state.get("status", "unknown")

                            if post_status in ("login", "signup"):
                                _step(step, f"Post-submit page is a {post_status} page — handling auth...")
                                logged_in, step = _handle_login_page(
                                    page, client, job_id, apply_url, step, result)
                                if logged_in:
                                    step += 1
                                    _step(step, "Logged in — restarting application...")
                                    page.goto(apply_url, wait_until="load", timeout=60000)
                                    page.wait_for_timeout(3000)
                                    page_num = 0
                                    continue
                                else:
                                    result["error"] = result.get("error") or "Login failed after Apply click"
                                    result["application_result"] = "failed"
                                    break
                            else:
                                _step(step, "Validation errors detected on page after submit")
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
                            # Strategy 3: Groq Vision fallback
                            step += 1
                            _step(step, "No clear success text -- verifying with Groq Vision...")
                            post = _ask_grok_vision_for_page_state(client, shot)
                            vis_status = post.get("status", "unknown")
                            msg = post.get("message", "")

                            if vis_status == "success":
                                step += 1
                                _step(step, f"Groq confirms success: {msg}")
                                result["success"] = True
                                result["application_result"] = "success"
                                # Rename screenshot to success
                                success_shot = _screenshot(page, job_id, "submission_success")
                                result["screenshots"].append(str(success_shot))
                            elif vis_status == "error":
                                step += 1
                                _step(step, f"Groq detected error: {msg}")
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

                    guidance = _ask_grok_vision_for_page_state(client, shot)
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
                        _step(step, f"Groq found button: \"{btn}\" -- clicking...")
                        try:
                            _click_button(page, btn)
                            page.wait_for_timeout(2000)
                            continue
                        except Exception as e:
                            _step(step, f"Could not click \"{btn}\": {e}")
                    elif status in ("login", "signup"):
                        step += 1
                        _step(step, f"Session expired — {status} page detected mid-form")
                        logged_in, step = _handle_login_page(
                            page, client, job_id, apply_url, step, result)
                        if logged_in:
                            step += 1
                            _step(step, "Re-logged in — navigating back to job page...")
                            page.goto(apply_url, wait_until="load", timeout=60000)
                            page.wait_for_timeout(3000)
                            page_num = 0  # restart form from page 1
                            continue
                        else:
                            result["error"] = result.get("error") or "Re-login failed mid-form"
                            break
                    elif status == "2fa":
                        step += 1
                        _step(step, "2FA required mid-form...")
                        from core.credential_manager import resolve_platform_key
                        pk = resolve_platform_key(page.url)
                        success_2fa, step = _handle_2fa(
                            page, client, job_id, pk, step, result)
                        if success_2fa:
                            page.goto(apply_url, wait_until="load", timeout=60000)
                            page.wait_for_timeout(3000)
                            page_num = 0
                            continue
                        else:
                            result["error"] = "2FA verification failed mid-form"
                            break
                    elif status == "captcha":
                        step += 1
                        _step(step, "CAPTCHA detected — cannot proceed")
                        result["error"] = "CAPTCHA challenge encountered"
                        break
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
#  Helpers that need the Groq client
# ═══════════════════════════════════════════════════════════════════════════════

def _find_and_click_apply_button_on_page(
    page: Page, client: OpenAI, job_id: str, step: int, result: dict
) -> tuple[bool, int]:
    """Search for Apply button via DOM, then Groq Vision fallback."""
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

    # Groq Vision fallback
    step += 1
    _step(step, "No standard Apply button found. Asking Groq Vision...")
    shot = _screenshot(page, job_id, "02_apply_btn_search")
    result["screenshots"].append(str(shot))
    b64 = _image_to_base64(shot)

    try:
        raw = _ask_grok_vision(
            client, b64,
            "I'm looking at a job posting page. I need to find the Apply button. "
            "Is there an Apply / Apply Now / Submit Application button visible? "
            "Respond with JSON: {\"found\": true/false, \"button_text\": \"...\", \"description\": \"...\"}"
        )
        info = _parse_json_response(raw)
        if info.get("found"):
            btn_text = info.get("button_text", "Apply")
            step += 1
            _step(step, f"Groq found Apply button: \"{btn_text}\" -> clicking")
            _click_button(page, btn_text)
            page.wait_for_timeout(1500)
            return True, step
    except Exception as e:
        logger.debug(f"Groq Vision apply button detection failed: {e}")

    step += 1
    _step(step, "No Apply button found -- assuming form is already visible")
    return False, step


def _ask_grok_vision_for_page_state(client: OpenAI, screenshot_path: Path) -> dict:
    """Ask Groq Vision to analyze the current page state."""
    b64 = _image_to_base64(screenshot_path)
    try:
        raw = _ask_grok_vision(
            client, b64,
            "What is the current state of this page? Is this:\n"
            "- A success/confirmation page (application submitted)?\n"
            "- An error page?\n"
            "- A page with a button to proceed (what text)?\n"
            "- A login page (email/password fields, sign-in form)?\n"
            "- A signup/registration page (create account form)?\n"
            "- A 2FA/MFA verification page (code input, OTP)?\n"
            "- A CAPTCHA challenge page?\n"
            "- Something else?\n\n"
            "Respond with JSON: {\"status\": \"success|error|has_button|login|signup|2fa|captcha|unknown\", "
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
    client: OpenAI,
    job_id: str,
    cover_letter: str,
    answers: dict,
    result: dict,
    step: int,
    auto_submit: bool = False,
    content_generator: "ContentGenerator | None" = None,
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

        # ── 2. Ask Groq Vision for field list ───────────────────────────
        step += 1
        _step(step, "Sending modal screenshot to Groq Vision for fields...")
        form_analysis = _identify_fields(client, shot)
        fields = form_analysis.get("fields", [])
        _step(step, f"Groq found {len(fields)} fields in modal step {modal_page}")

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
            _, step = _fill_field(page, field, value, step, cover_letter=cover_letter,
                                  content_generator=content_generator)

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
            # Groq Vision final check
            state = _ask_grok_vision_for_page_state(client, shot_final)
            if state.get("status") == "success":
                _step(step, "Groq Vision confirmed: application submitted")
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
