# JobTracker Enhancement Phase — Implementation Plan

## Context
JobTracker currently scans LinkedIn/HireMeTech/WhatsApp for student/junior positions in Israel, scores them with Claude, and notifies the user via Twilio WhatsApp with YES/NO/SKIP commands. YES immediately triggers a blind auto-apply with no user input. The system has no conversation state, no API auth, and limited form recovery. This plan evolves it into a feedback-driven, more secure, more robust pipeline.

---

## Developer Split

Two parallel feature branches, minimal file overlap:

| Branch | Owner | Files |
|--------|-------|-------|
| `feature/whatsapp-feedback-logic` | Dev A | `webhook.py`, `core/notifier.py`, `db/models.py` (add ConversationState), LinkedIn Easy Apply in `scanners/linkedin.py` |
| `feature/applicator-hardening` | Dev B | `core/applicator.py`, `api.py`, `db/models.py` (add ATS memory table), `data/default_answers.yaml`, `config/profile.yaml` |

`db/models.py` has minor changes in both branches — coordinate a shared migration PR to merge first.

---

## Part 1 — Feedback-Driven WhatsApp Loop (Dev A: `feature/whatsapp-feedback-logic`)

### Problem
YES immediately applies. User cannot guide the application (e.g., "emphasize Docker", "wait, update my CV first").

### New Conversation Flow
```
System sends suggestion card
  ↓
User replies "YES"
  → System sends: "🎯 כל הערות לפני שאגיש? (ענה 'כן' להגשה ישירה, או כתוב הנחיה)"
  ↓
User replies "כן" / "yes" / "go" / "submit"  → apply with no extra instruction
User replies free text                         → apply with that instruction injected into cover letter
User replies "WAIT" / "המתן"                  → status stays "approved", no apply yet
```

### Implementation Steps

**1. Add `ConversationState` table to `db/models.py`**
```python
class ConversationState(Base):
    __tablename__ = "conversation_state"
    id = Column(Integer, primary_key=True)
    state = Column(String, default="idle")  # idle | awaiting_feedback
    pending_job_hash = Column(String)        # job hash waiting for feedback
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
```
Single-row table (one user). `init_db()` seeds it if absent.

**2. Refactor `webhook.py`**

Current `_handle_yes()` → split into two stages:

- **Stage 1** (`_handle_yes()`): Set `SuggestedJob.status="approved"`, set conversation state to `awaiting_feedback`, set `pending_job_hash`. Reply: `"🎯 כל הערות לפני שאגיש? (ענה 'כן' להגשה ישירה, או כתוב הנחיה כגון 'הדגש את Docker')"`

- **Stage 2** (new `_handle_feedback(text)`): Called when `state == awaiting_feedback`. Parses `text`:
  - If "כן"/"yes"/"go"/"submit"/empty → call `_apply_with_feedback(job_hash, instruction=None)`
  - If "WAIT"/"המתן" → reply "⏸ בסדר, לא מגיש עכשיו. שלח 'כן' כשתהיה מוכן." leave state as-is
  - Otherwise → call `_apply_with_feedback(job_hash, instruction=text)`

- **State reset** after apply or on NO/SKIP.

**3. Thread feedback through to `_generate_cover_letter` in `core/applicator.py`**

Add optional `user_instruction: str = ""` param to `apply_to_job()` and `_generate_cover_letter()`.

In `_generate_cover_letter()`: if `user_instruction` is non-empty, append to the Claude prompt:
```
User instruction: "{instruction}" — incorporate this emphasis into the letter.
```

The `apply_to_job()` signature in `main.py` must be updated accordingly.

**4. Confirmation message before applying**

After receiving the instruction (Stage 2), before spawning the thread, send a WhatsApp confirmation:
```
"⚙️ מגיש ל-{company} עם הנחיה: '{instruction}'\nזה יקח כמה שניות..."
```
(Or without instruction if none given.)

---

## Part 2 — Security & API Auth (Dev B: `feature/applicator-hardening`)

### Scope (pragmatic for a student project)

**Skip**: secret vault, OS-level encryption — overkill for local single-user setup.
**Do**: API key auth on Flask API (prevents open dashboard in any network), file permissions for linkedin_session.json.

**2a. API Key Auth on `api.py`**

- Add `API_KEY` to `.env.example` (generate with `python -c "import secrets; print(secrets.token_hex(32))"`)
- Add middleware to `api.py`:
```python
from functools import wraps

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if key != os.environ.get("API_KEY"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated
```
- Apply `@require_api_key` to all `/api/*` routes.
- Update `dashboard/.env.example`: add `NEXT_PUBLIC_API_KEY=...`
- Update `dashboard/lib/api.ts`: add `X-API-Key` header to all fetch calls.
- `/health` stays unauthenticated.

**2b. LinkedIn session file permissions**

In `scanners/linkedin.py`, after writing `SESSION_FILE`:
```python
import stat, os
SESSION_FILE.chmod(0o600)  # owner read/write only — no-op on Windows but correct on Linux/Mac
```

**2c. No plaintext LinkedIn password in memory longer than needed**

Currently credentials are loaded at module level. No change needed — they're already in env vars, not hardcoded.

---

## Part 3 — Advanced Form Handling (Dev B: `feature/applicator-hardening`)

### 3a. Structure Memory (ATS Layout Cache)

**Problem**: Same ATS (e.g., Comeet) appears repeatedly. Current code re-discovers field mappings every time via Claude Vision (slow + costs tokens).

**New table `ats_field_memory` in `db/models.py`**:
```python
class ATSFieldMemory(Base):
    __tablename__ = "ats_field_memory"
    id = Column(Integer, primary_key=True)
    ats_key = Column(String, unique=True, index=True)  # e.g. "comeet", "greenhouse"
    field_mappings = Column(JSON)   # {canonical_field: css_selector}
    success_count = Column(Integer, default=1)
    last_used = Column(DateTime)
```

**ATS key extraction**: parse from apply_url hostname (already done partially in `_extract_from_url` in `whatsapp_bridge.py` — reuse that logic).

**Usage in `apply_to_job()`**:
1. Before calling Claude Vision for field identification, check `ats_field_memory` for the ATS key.
2. If found (and `success_count >= 2`), try cached CSS selectors first. If they work, skip Claude Vision call.
3. After a successful submission, upsert the discovered mappings into `ats_field_memory`.

**Files**: `core/applicator.py`, `db/models.py`, `db/database.py` (init_db)

### 3b. WhatsApp Fallback for Unrecognized Required Fields

**Problem**: Some forms have behavioral questions ("Tell us about a challenge you overcame") that the 4-strategy cascade can't fill from `default_answers.yaml`.

**New flow** (in `core/applicator.py`):
- If Strategy 4 fails for a **required** field AND field type is `textarea`:
  1. Take a screenshot of the field
  2. Send WhatsApp message: `"❓ נתקלתי בשאלה שאני לא יודע לענות עליה:\n\"{field_label}\"\nשלח לי תשובה ואמשיך."`
  3. **Pause** the apply thread — poll `ConversationState` for `state == "pending_field_answer"` with a 5-minute timeout
  4. On timeout: skip the field, log warning, continue
  5. Dev A needs to add `state = "pending_field_answer"` + `pending_field_label` to `ConversationState` and a new webhook handler

**Coordination point**: Dev A adds the webhook handler, Dev B adds the polling logic. Define the shared interface: `ConversationState.state = "field_answer_ready"` + `ConversationState.field_answer = "..."`.

---

## Part 4 — Suggested Additional Features & Fixes

### Fix 1: Legacy `Job` Model Removal (Dev B)
- `db/models.py` has a `Job` table (legacy). No code path creates new `Job` records — all new logic uses `SuggestedJob` and `Application`.
- **Action**: Add a `_migrate_legacy()` step in `database.py` that copies any remaining `Job` records into `SuggestedJob` (if hash not present), then drops the `jobs` table.
- Removes confusion and dead code paths.

### Fix 2: `Application` → `SuggestedJob` FK link (Dev B)
- Currently `Application.job_hash` links to `SuggestedJob.job_hash` but this is not enforced by a FK constraint. `api.py` stats don't cross-reference.
- **Action**: Add `ForeignKey("suggested_jobs.job_hash")` to `Application.job_hash` and add a `relationship` for joined queries. Update `/api/stats` to include success rate per source.

### Fix 3: LinkedIn Easy Apply (Dev A)
- `scanners/linkedin.py` currently skips "Easy Apply" LinkedIn jobs.
- **Action**: Add a specialized `apply_linkedin_easy_apply(page, profile, answers, cover_letter)` handler in `core/applicator.py`:
  - Detect "Easy Apply" button on LinkedIn job page
  - Fill the LinkedIn-native multi-step modal (name, email, phone, resume upload, screening questions)
  - Use existing `_fill_field()` strategies — LinkedIn modal uses standard HTML inputs
  - On success: record `application_method = "easy_apply"` in `Application`
- Update `scanners/linkedin.py` to pass Easy Apply jobs through the pipeline rather than skipping.

### Fix 4: CV Versioning (Dev B)
- Allow multiple CV files in `data/`: `CV Resume.pdf`, `CV-Backend.pdf`, `CV-DevOps.pdf`, etc.
- Store `cv_variant` in `SuggestedJob` (new nullable column).
- In feedback loop (Dev A): if user says "use DevOps CV", the instruction parser sets `cv_variant = "CV-DevOps"` which `apply_to_job()` resolves to the matching file path.
- **Coordination**: Dev A parses cv variant from feedback text; Dev B resolves path in applicator.

### Fix 5: Error Recovery via WhatsApp Screenshot (Dev B)
- When `apply_to_job()` hits an unrecoverable selector error, it already saves a screenshot via `application_result = "failed"` + `screenshot_path`.
- **Add**: In the failed-apply notification already sent by the webhook thread, attach the screenshot image via Twilio media URL or embed the path.
- Update `core/notifier.py`: add `send_failure_screenshot(job, screenshot_path)` that sends a Twilio WhatsApp message with `MediaUrl` pointing to the screenshot (requires a public URL — use ngrok or local file served briefly via Flask).
- Simpler fallback: send the text content of `error_message` with the screenshot path so the user can find it locally.

### Fix 6: `core/scheduler.py` is a stub
- Implement using the already-installed `apscheduler`:
  - Scan every 12h (HireMeTech + LinkedIn)
  - Expire every 1h
  - Re-notify skipped jobs when `expires_at < now` and `status="skipped"`

---

## Critical Files

| File | Changes |
|------|---------|
| `webhook.py` | Feedback state machine, new `_handle_feedback()`, polling for field answers |
| `core/notifier.py` | New `send_failure_screenshot()`, updated card format for feedback prompt |
| `core/applicator.py` | `user_instruction` param in `apply_to_job()` + `_generate_cover_letter()`, ATS memory cache, field fallback polling, Easy Apply handler, CV versioning |
| `db/models.py` | Add `ConversationState`, `ATSFieldMemory`, FK on `Application.job_hash`, `cv_variant` on `SuggestedJob`, drop legacy `Job` |
| `db/database.py` | `init_db()` seeds `ConversationState`; legacy migration |
| `api.py` | `require_api_key` decorator on all `/api/*` routes |
| `dashboard/lib/api.ts` | Add `X-API-Key` header |
| `scanners/linkedin.py` | File chmod after session save; Easy Apply pass-through |
| `.env.example` | Add `API_KEY=` |

---

## Verification

1. **Feedback loop**: Send "YES" via WhatsApp → verify `state=awaiting_feedback` in DB → reply "הדגש Python" → verify cover letter contains the instruction → verify `Application` record created.
2. **API auth**: `curl http://localhost:5001/api/stats` → 401. With `X-API-Key: <key>` → 200.
3. **ATS memory**: Apply to two jobs on same ATS → second application should skip Vision call (check logs: "Using cached ATS mapping for comeet").
4. **Field fallback**: Manually test a form with a behavioral textarea → verify WhatsApp message received.
5. **Easy Apply**: Run LinkedIn scan, find an Easy Apply job → verify application_method = "easy_apply" in DB.
6. **Legacy cleanup**: `SELECT * FROM jobs` after migration → empty or table dropped.
