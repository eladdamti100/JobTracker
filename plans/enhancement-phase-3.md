# JobTracker Phase 3 — Adapter Expansion Plan

## Context
The project already has a complete 9-state orchestrator FSM, 5 working adapters (Workday, Amazon, Greenhouse, Lever, Generic), encrypted credentials, session caching, ATS field memory, and full WhatsApp conversation flow. Estimated current coverage is ~90% with GenericAdapter+Vision as the safety net.

**Goal**: Reach 85-90% *deterministic* coverage (no Vision needed for common sites) + harden security. Each developer builds their own adapter files with zero merge conflicts.

---

## Developer Split

| Branch | Owner | Goal |
|--------|-------|------|
| `adapters-il` | Dev A (you) | Israeli market: LinkedIn Easy Apply + Comeet |
| `adapters-global` | Dev B (friend) | Global ATS: SmartRecruiters + Security hardening |

Only shared files: `core/adapters/__init__.py` (each adds 1 import line) and `webhook.py` (each touches a different function). No conflicts.

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

### Branch A Verification
1. `assert LinkedInAdapter.detect("https://www.linkedin.com/jobs/view/123")`
2. `assert ComeetAdapter.detect("https://careers.comeet.co/jobs/wix/123")`
3. Verify both appear in `_ADAPTER_REGISTRY`
4. Dry-run (`auto_submit=False`) on a known LinkedIn Easy Apply URL → reaches `FILL_FORM`, screenshots saved
5. Dry-run on a Comeet URL → form fields detected
6. Force HUMAN_INTERVENTION state → WhatsApp message arrives → send DONE → checkpoint resumes without state being wiped

---

## Branch B: `adapters-global`

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

### Branch B Verification
1. `assert SmartRecruitersAdapter.detect("https://jobs.smartrecruiters.com/Amdocs/123")`
2. CORS: API response header should show `localhost:3000`, not `*`
3. API without `API_KEY` + `DEV_MODE=false` → 500
4. API with wrong key → 401; correct key → 200
5. POST to `/webhook` with bad Twilio signature → 403
6. SmartRecruiters dry-run: reaches `submit` state, screenshots saved

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
