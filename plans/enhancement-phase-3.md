# JobTracker Phase 3 — Adapter Expansion Plan

## Context
The project already has a complete 9-state orchestrator FSM, 5 working adapters (Workday, Amazon, Greenhouse, Lever, Generic), encrypted credentials, session caching, ATS field memory, and full WhatsApp conversation flow. Estimated current coverage is ~90% with GenericAdapter+Vision as the safety net.

**Goal**: Reach 85-90% *deterministic* coverage (no Vision needed for common sites) + harden security. Each developer builds their own adapter files with zero merge conflicts.

---

## Developer Split

| Branch | Owner | Verify existing | Build new |
|--------|-------|-----------------|-----------|
| `adapters-il` | Dev A (you) | WorkdayAdapter + GenericAdapter | LinkedIn Easy Apply + Comeet |
| `adapters-global` | Dev B (friend) | GreenhouseAdapter + LeverAdapter | SmartRecruiters + Security hardening |

Only shared files: `core/adapters/__init__.py` (each adds 1 import line) and `webhook.py` (each touches a different function). No conflicts.

> **Rule**: Before writing any new code, each dev must run the verification checklist for their assigned existing adapters and fix any bugs found. Only proceed to building new adapters once all verification steps pass.

---

## Coverage Improvement Per Adapter

| Adapter | Key Israeli Companies | Gain |
|---------|----------------------|------|
| LinkedInAdapter | All "Easy Apply" LinkedIn jobs — primary source | +15-20% |
| ComeetAdapter | Wix, Monday, IronSource, Fiverr, AppsFlyer, Outbrain | +8-12% |
| SmartRecruitersAdapter | Amdocs, NICE Systems, Vonage Israel | +3-5% |
| Security fixes | — | coverage unchanged, security hardened |

---

## Branch A: `adapters-il`

### Step A0 — Verify existing adapters: WorkdayAdapter + GenericAdapter

Complete all checks below before writing any new code. Log failures as issues on the branch.

---

#### A0.1 — WorkdayAdapter (`core/adapters/workday_adapter.py`)

**Detection tests (no browser needed):**
```python
from core.adapters.workday_adapter import WorkdayAdapter

assert WorkdayAdapter.detect("https://company.wd1.myworkdayjobs.com/en-US/External/job/123")
assert WorkdayAdapter.detect("https://company.wd3.myworkdayjobs.com/careers/job/456")
assert WorkdayAdapter.detect("https://wd1.myworkdayjobs.com/company/job/789")
assert not WorkdayAdapter.detect("https://greenhouse.io/company/job/123")
assert "workday" in __import__("core.orchestrator", fromlist=["_ADAPTER_REGISTRY"])._ADAPTER_REGISTRY
```

**Dry-run checklist** (`auto_submit=False`, real Workday URL):
- [ ] `plan()` navigates to URL and returns `FILL_FORM` (not `LOGIN`) when session cookie exists
- [ ] `plan()` returns `LOGIN` when no session — then `login()` runs, session saved to `data/workday_<tenant>_session.json`
- [ ] `fill_form()` fills `data-automation-id="legalNameSection_firstName"` correctly (check screenshot)
- [ ] `fill_form()` fills `data-automation-id="legalNameSection_lastName"` correctly
- [ ] `fill_form()` fills `data-automation-id="phone-number"` correctly
- [ ] Multi-step navigation: `bottom-navigation-next-btn` clicked for each page, no infinite loop
- [ ] Resume uploaded (`input[data-automation-id="file-upload-block"]` or equivalent)
- [ ] Stops before submit when `auto_submit=False` — screenshot saved as `wd_pre_submit_<hash>.png`
- [ ] CAPTCHA detected → `request_human_intervention()` called, WhatsApp message arrives
- [ ] `cleanup()` closes browser — no dangling Playwright processes

**Known edge cases to test:**
- [ ] Workday "Save and Continue" button (`bottom-navigation-save-continue-btn`) appears on some tenants — verify it is clicked instead of Next
- [ ] Country/dropdown fields (`select[data-automation-id*="country"]`) — verify a value is selected, not left blank

---

#### A0.2 — GenericAdapter (`core/adapters/generic_adapter.py`)

**Detection tests:**
```python
from core.adapters.generic_adapter import GenericAdapter
from core.orchestrator import _ADAPTER_REGISTRY

# Generic must NOT detect URLs handled by specific adapters
assert not GenericAdapter.detect("https://greenhouse.io/job/123")  # only if detect() is defined
# Generic is the catch-all — registered last, matched when no other adapter fires
assert _ADAPTER_REGISTRY.get("generic") is not None or \
       _ADAPTER_REGISTRY.get("*") is not None  # adjust to actual key used
```

**DOM-first + Vision-fallback dry-run** (use any job URL with no dedicated adapter):
- [ ] `plan()` opens browser, navigates, returns a valid `ApplyState` (not exception)
- [ ] `_dom_detect_page_state()` correctly classifies the page as `form`, `apply_button`, or `login`
- [ ] `fill_form()` fills at least email field via DOM selectors before falling back to Vision
- [ ] `_vision_identify_fields()` is called only when DOM detection returns 0 known fields
- [ ] Vision call result is applied: fields returned by Groq are filled if selectors are valid
- [ ] Screenshot saved after each step (`generic_form_start_<hash>.png`, etc.)
- [ ] `auto_submit=False` stops before clicking submit — screenshot shows filled form

**Fallback integration:**
- [ ] When no other adapter matches a URL, `GenericAdapter` is selected by orchestrator (confirm by checking log: `"Using adapter: generic"`)
- [ ] A URL handled by `GreenhouseAdapter` does NOT fall back to `GenericAdapter`

---

**Files to create/modify:**
- `core/adapters/linkedin_adapter.py` — NEW
- `core/adapters/comeet_adapter.py` — NEW
- `core/adapters/__init__.py` — add 2 imports (before generic_adapter)
- `webhook.py` — fix HUMAN_INTERVENTION state leak in `_spawn_apply_thread`

### Adapters: Pattern to Follow
Base all new adapters on `LeverAdapter` — it's the simplest complete adapter. Every adapter:
- Inherits `GenericAdapter` (from `core/adapters/generic_adapter.py`)
- Implements: `plan()`, `restore_session()`, `login()`, `signup()`, `verify()`, `fill_form()`, `review()`, `submit()`, `cleanup()`
- Self-registers at bottom: `register_adapter("key", ClassName)`
- Reuses: `_open_browser()`, `_safe_screenshot()`, `_fill_input()`, `_upload_file()`, `_check_consent_checkboxes()`, `_vision_identify_fields()` from GenericAdapter/BaseAdapter
- For unknown custom questions: fall back to `_vision_identify_fields()` (already in GenericAdapter)
- For CAPTCHA/MFA: call `request_human_intervention()` from `core/verifier.py`

---

### Step A1 — Fix HUMAN_INTERVENTION state leak in `webhook.py`

**Problem**: `_spawn_apply_thread()`'s `finally` block always resets `ConversationState` to `"idle"`, which wipes the `pending_intervention` state before the user can send DONE.

**Fix in `webhook.py`** — wrap the `finally` state reset:
```python
finally:
    from core.orchestrator import ApplyState
    if result is None or result.final_state != ApplyState.HUMAN_INTERVENTION:
        _set_conversation_state("idle")
    # If HUMAN_INTERVENTION: leave state as-is; _handle_done() will reset it
```

Also add a 3-branch check instead of 2 in the result handler:
```python
if result.success:
    send_whatsapp(f"✅ הוגש! {company} — {title}")
elif result.final_state == ApplyState.HUMAN_INTERVENTION:
    pass  # verifier.request_human_intervention() already sent the WhatsApp message
else:
    send_whatsapp(f"❌ נכשל: {company} — {title}\n{result.error or ''}")
```

---

### Step A2 — `core/adapters/linkedin_adapter.py`

**Platform key**: `"linkedin"`
**Detection**: `"linkedin.com" in url`
**Session**: Load from `data/linkedin_session.json` (already managed by scanner) — no fresh login needed in most cases.

**Key selectors**:
```python
EASY_APPLY_BTN  = "[aria-label*='Easy Apply' i]"
MODAL           = "div.jobs-easy-apply-modal"
NEXT_BTN        = "button[aria-label='Continue to next step']"
REVIEW_BTN      = "button[aria-label='Review your application']"
SUBMIT_BTN      = "button[aria-label='Submit application']"
PHONE_FIELD     = "#phoneNumber-nationalNumber"
```

**`plan()`**: Load session → navigate → detect Easy Apply button → return `FILL_FORM`. If login wall: return `LOGIN`.

**`fill_form()`** — multi-step modal loop (up to 10 steps):
1. Click Easy Apply button
2. Wait for modal: `div.jobs-easy-apply-modal`
3. Each step: screenshot → fill visible inputs by label→value matching → handle file upload → click Next/Review/Submit
4. Contact info fields: phone, city — filled from `default_answers.yaml`
5. Resume: `input[type='file']` → upload CV
6. Screening questions: generic label→input match using `_fill_input()`
7. If CAPTCHA detected → `request_human_intervention()`

**`submit()`**: Click `button[aria-label='Submit application']` → wait → detect modal close or "Application submitted" text → return `SUCCESS`.

**`login()`**: Load `data/linkedin_session.json` via `context.add_cookies()`. If session expired, use `LINKEDIN_EMAIL` from env → standard email/password flow → save new session.

**Add to `PLATFORM_PATTERNS`** in `core/credential_manager.py`:
```python
"linkedin.com": "linkedin",
```

---

### Step A3 — `core/adapters/comeet_adapter.py`

**Platform key**: `"comeet"`
**Detection**: `any(d in url for d in ("comeet.co", "careers.comeet", "recruiting.comeet"))`
**Auth**: Usually anonymous (no login required).

**Key selectors**:
```python
APPLY_BTN = "a[data-automation-id='btn-apply'], button.apply-button, a:has-text('Apply')"
FIELDS = {
    "first_name":   "input[placeholder*='First' i], input[name*='first' i]",
    "last_name":    "input[placeholder*='Last' i], input[name*='last' i]",
    "email":        "input[type='email']",
    "phone":        "input[type='tel']",
    "resume":       "input[type='file']",
    "cover_letter": "textarea[name*='cover' i], textarea[placeholder*='cover' i]",
    "linkedin":     "input[placeholder*='linkedin' i]",
    "questions":    "div.comeet-question, div[class*='question-wrapper']",
}
SUBMIT_BTN = "button[type='submit'], button:has-text('Submit'), button:has-text('Apply')"
```

**`fill_form()`**:
1. Click Apply button if on listing page
2. Wait for `input[type='email']`
3. Fill first_name, last_name, email, phone in order
4. Upload resume
5. Fill cover_letter if visible
6. Fill LinkedIn URL if visible
7. Loop `div.comeet-question` containers → extract label → normalize → lookup → fill
8. `_check_consent_checkboxes(page, 1)`
9. Screenshot → return `next_state="submit"`

**`submit()`**: Click first visible submit selector → wait 4s → detect: URL contains "confirmation/success/thank" OR `div[class*='success']` visible OR body text "thank you / application received".

**Add to `PLATFORM_PATTERNS`** in `core/credential_manager.py`:
```python
"comeet.co": "comeet",
```

---

### Step A4 — Register in `core/adapters/__init__.py`

Add these two imports **before** `generic_adapter`:
```python
from core.adapters import linkedin_adapter  as _linkedin  # noqa: F401
from core.adapters import comeet_adapter    as _comeet    # noqa: F401
```

---

### Branch A — Done Criteria

**Phase 1: Existing adapters verified** (Step A0 complete)
- [ ] All WorkdayAdapter checklist items pass
- [ ] All GenericAdapter checklist items pass
- [ ] Any bugs found are fixed and committed on this branch

**Phase 2: New adapters built and verified** (Steps A1–A4 complete)
- [ ] `assert LinkedInAdapter.detect("https://www.linkedin.com/jobs/view/123")`
- [ ] `assert ComeetAdapter.detect("https://careers.comeet.co/jobs/wix/123")`
- [ ] Both appear in `_ADAPTER_REGISTRY`
- [ ] Dry-run on LinkedIn Easy Apply URL → reaches `FILL_FORM`, screenshots saved in `data/screenshots/`
- [ ] Dry-run on Comeet URL → all visible form fields filled, screenshot shows pre-submit state
- [ ] HUMAN_INTERVENTION state fix: force intervention → WhatsApp message arrives → send DONE → state not wiped, checkpoint resumes

---

## Branch B: `adapters-global`

### Step B0 — Verify existing adapters: GreenhouseAdapter + LeverAdapter

Complete all checks below before writing any new code. Log failures as issues on the branch.

---

#### B0.1 — GreenhouseAdapter (`core/adapters/greenhouse_adapter.py`)

**Detection tests (no browser needed):**
```python
from core.adapters.greenhouse_adapter import GreenhouseAdapter

assert GreenhouseAdapter.detect("https://boards.greenhouse.io/company/jobs/123")
assert GreenhouseAdapter.detect("https://grnh.se/abc123def")   # Greenhouse short-links
assert not GreenhouseAdapter.detect("https://jobs.lever.co/company/123")
assert not GreenhouseAdapter.detect("https://company.wd1.myworkdayjobs.com/job/123")
```

**Dry-run checklist** (`auto_submit=False`, real Greenhouse URL):
- [ ] `plan()` navigates to a `boards.greenhouse.io` URL and returns `FILL_FORM` (no login needed)
- [ ] `fill_form()` fills `#first_name`, `#last_name`, `#email`, `#phone` (verify via screenshot)
- [ ] Resume uploaded via `#resume` or `input[name='resume']`
- [ ] Cover letter filled if `#cover_letter` is visible
- [ ] LinkedIn URL filled if `input[id*='linkedin' i]` is present
- [ ] Custom questions loop (`li.custom-field`): for each question block, label extracted + answer looked up + field filled
- [ ] `_check_consent_checkboxes(page, 1)` runs without error
- [ ] CAPTCHA detected → `request_human_intervention()` called, WhatsApp message arrives
- [ ] Stops before submit when `auto_submit=False` — screenshot `gh_pre_submit_<hash>.png` saved
- [ ] `_detect_greenhouse_success()` returns True when confirmation div `#application-confirmation` is present (can simulate by loading the confirmation URL directly)

**Edge cases:**
- [ ] `grnh.se` short-link: verify the redirect is followed and `GreenhouseAdapter` is still selected (not `GenericAdapter`)
- [ ] Custom question with `<select>`: `_select_option()` picks the best-matching option, not blank
- [ ] Custom question with radio buttons: `_handle_radio_group()` selects the matching option

---

#### B0.2 — LeverAdapter (`core/adapters/lever_adapter.py`)

**Detection tests (no browser needed):**
```python
from core.adapters.lever_adapter import LeverAdapter

assert LeverAdapter.detect("https://jobs.lever.co/company/abc123-uuid")
assert LeverAdapter.detect("https://lever.co/company/jobs/abc123")
assert not LeverAdapter.detect("https://boards.greenhouse.io/company/jobs/123")
assert not LeverAdapter.detect("https://smartrecruiters.com/company/job/123")
```

**Dry-run checklist** (`auto_submit=False`, real Lever URL):
- [ ] `plan()` navigates to `jobs.lever.co` URL and returns `FILL_FORM`
- [ ] If on job listing page (not form), `_click_lever_apply_button()` clicks Apply and transitions to form
- [ ] `fill_form()` fills `input[name="name"]` with full name (first + last joined)
- [ ] `fill_form()` fills `input[name="email"]` and `input[name="phone"]`
- [ ] Resume uploaded via `input[name="resume"]` or `input[type="file"][id*="resume" i]`
- [ ] Cover letter filled in `textarea[name="comments"]` if visible
- [ ] Social links filled: `input[name="urls[LinkedIn]"]`, `input[name="urls[GitHub]"]` (if visible)
- [ ] Custom questions loop (`div.application-question`): core fields are skipped, remaining questions answered
- [ ] `_check_consent_checkboxes(page, 1)` runs without error
- [ ] CAPTCHA detected → `request_human_intervention()` called, WhatsApp message arrives
- [ ] Stops before submit when `auto_submit=False` — screenshot `lv_pre_submit_<hash>.png` saved
- [ ] `_detect_lever_success()` returns True for `div.thanks` or body text "thanks for applying"

**Edge cases:**
- [ ] Lever "Apply for this job" button text vs. "Apply Now" — `_click_lever_apply_button()` handles both
- [ ] Custom question whose container also holds `input[name="email"]` is skipped (dedup check in `_fill_lever_custom_questions`)
- [ ] Social link fields not present on some jobs: code does not raise, just skips

---

**Files to create/modify:**
- `core/adapters/smartrecruiters_adapter.py` — NEW
- `core/adapters/icims_adapter.py` — NEW (lower priority)
- `core/adapters/__init__.py` — add 1-2 imports
- `api.py` — remove dev-mode bypass, fix CORS
- `webhook.py` — add Twilio signature validation
- `.env.example` — add missing vars

---

### Step B1 — `core/adapters/smartrecruiters_adapter.py`

**Platform key**: `"smartrecruiters"` (already in `PLATFORM_PATTERNS` in `credential_manager.py`)
**Detection**: `"smartrecruiters.com" in url`
**Email verification**: `email_verifier.py` already has SmartRecruiters senders configured.

**Key selectors** (SmartRecruiters uses stable `data-qa` + `name=` attributes):
```python
SR = {
    "apply_btn":    '[data-qa="btn-apply"]',
    "first_name":   'input[name="firstName"]',
    "last_name":    'input[name="lastName"]',
    "email":        'input[name="email"]',
    "phone":        'input[name="phoneNumber"], input[name="phone"]',
    "resume":       'input[type="file"]',
    "cover_letter": 'textarea[name="message"]',
    "linkedin":     'input[name="web.LinkedInUrl"]',
    "questions":    'fieldset[data-qa="field"]',
    "submit":       'button[data-qa="btn-apply-apply"], button[type="submit"]',
}
```

**`fill_form()`**: Click apply btn → wait for `input[name="firstName"]` → fill all SR fields → loop `fieldset[data-qa="field"]` for custom questions → consent checkboxes → return `submit`.

**`verify()`**: Call `auto_verify("smartrecruiters")` from `core/email_verifier.py`. If link type → navigate to it. If code type → fill OTP input. If IMAP fails → `request_human_intervention()`.

**`submit()`**: Click `[data-qa="btn-apply-apply"]` → detect `[data-qa="confirmation-page"]` or "Thank you" in body.

---

### Step B2 — `core/adapters/icims_adapter.py` (lower priority)

**Platform key**: `"icims"` (already in `PLATFORM_PATTERNS`)
**Detection**: `"icims.com" in url`
**Auth**: Login required. Use `get_credential("icims")` → if none, `signup()`.

Multi-page wizard (3-4 pages). Each page: fill visible fields → click "Next"/"Continue" → loop until Submit visible.

Key selectors:
```python
IC = {
    "email":     'input[id*="email" i]',
    "password":  'input[type="password"]',
    "login":     'button[id*="login"], input[type="submit"]',
    "apply":     'a.iCIMS_MainNav_Apply, a:has-text("Apply Now")',
    "first":     'input[id*="firstname" i]',
    "last":      'input[id*="lastname" i]',
    "phone":     'input[id*="phone" i]',
    "resume":    'input[type="file"]',
    "next":      'button:has-text("Next"), button:has-text("Continue")',
    "submit":    'input[value="Submit"], button:has-text("Submit")',
    "success":   'div.iCIMS_InfoMsg_Job',
}
```

---

### Step B3 — Security fixes in `api.py`

1. **CORS**: Replace `CORS(app, origins="*")` with:
   ```python
   _origins = os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
   CORS(app, origins=[o.strip() for o in _origins])
   ```

2. **API key dev-mode bypass**: Replace the `if not expected: return f(...)` bypass with:
   ```python
   if not expected:
       if os.environ.get("DEV_MODE", "false").lower() == "true":
           return f(*args, **kwargs)
       return jsonify({"error": "API_KEY not configured"}), 500
   ```

---

### Step B4 — Twilio signature validation in `webhook.py`

Add before the `webhook()` route:
```python
from twilio.request_validator import RequestValidator

def _validate_twilio_signature() -> bool:
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not token:
        return True  # skip validation in dev
    v = RequestValidator(token)
    url = os.environ.get("WEBHOOK_BASE_URL", request.url)
    if os.environ.get("WEBHOOK_BASE_URL"):
        url = os.environ["WEBHOOK_BASE_URL"].rstrip("/") + "/webhook"
    return v.validate(url, request.form.to_dict(), request.headers.get("X-Twilio-Signature", ""))
```

Inside `webhook()` handler at the top:
```python
if not _validate_twilio_signature():
    logger.warning("Invalid Twilio signature — rejected")
    return "Forbidden", 403
```

---

### Step B5 — `.env.example` additions

```
# Required for encrypted credential storage
# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CREDENTIAL_ENCRYPTION_KEY=

# Dashboard API auth
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
API_KEY=
DEV_MODE=false

# CORS (comma-separated origins)
CORS_ORIGINS=http://localhost:3000

# Ngrok public URL (needed for Twilio signature validation)
WEBHOOK_BASE_URL=

# Gmail IMAP for OTP auto-read
GMAIL_ADDRESS=
GMAIL_APP_PASSWORD=
```

---

### Branch B — Done Criteria

**Phase 1: Existing adapters verified** (Step B0 complete)
- [ ] All GreenhouseAdapter checklist items pass
- [ ] All LeverAdapter checklist items pass
- [ ] Any bugs found are fixed and committed on this branch

**Phase 2: New adapter + security built and verified** (Steps B1–B5 complete)
- [ ] `assert SmartRecruitersAdapter.detect("https://jobs.smartrecruiters.com/Amdocs/123")`
- [ ] SmartRecruiters dry-run: all `[data-qa]` fields filled, reaches `submit` state, screenshots saved
- [ ] CORS: API response header shows `localhost:3000`, not `*`
- [ ] API without `API_KEY` + `DEV_MODE=false` → 500 error
- [ ] API with wrong key → 401; correct key → 200
- [ ] POST to `/webhook` with bad Twilio signature → 403

---

## Critical Files Reference

| File | Purpose |
|------|---------|
| `core/adapters/lever_adapter.py` | Best pattern to follow for new adapters |
| `core/adapters/generic_adapter.py` | Base class — inherit from this; `_fill_input()`, `_upload_file()`, `_vision_identify_fields()` |
| `core/verifier.py` | `request_human_intervention()` — call from adapters on CAPTCHA/MFA |
| `core/email_verifier.py` | `auto_verify(platform_key)` — call from `verify()` method |
| `core/credential_manager.py` | `get_credential()` / `save_credential()` — add new platform keys to `PLATFORM_PATTERNS` |
| `core/adapters/__init__.py` | Register all new adapters here — coordinate import order |
| `webhook.py` | Branch A: fix HUMAN_INTERVENTION state; Branch B: Twilio validation |
| `api.py` | Branch B: remove dev-mode bypass, fix CORS |
