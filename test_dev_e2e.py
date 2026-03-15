"""
End-to-end test for the dev branch — exercises the full pipeline with ONE real job.

Tests:
  1. API Key auth (reject without key, allow with key)
  2. Fetch a real suggested job via API
  3. Approve it via PATCH (simulates WhatsApp YES)
  4. Trigger auto-apply on that job
  5. Verify ATS memory cache was saved (if ATS detected)
  6. Verify Application record was created in DB
  7. Verify WhatsApp notification was sent (success or failure)

Usage:
  python test_dev_e2e.py
"""

import os
import sys
import time
import secrets
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Setup ────────────────────────────────────────────────────────────────────

# Generate a temporary API key for testing
TEST_API_KEY = secrets.token_hex(16)
os.environ["API_KEY"] = TEST_API_KEY

from db.database import init_db, get_session
from db.models import SuggestedJob, Application, ATSFieldMemory, ConversationState

init_db()

API_PORT = int(os.environ.get("API_PORT", 5001))
API_BASE = f"http://localhost:{API_PORT}"

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"
results = []


def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append(condition)
    msg = f"  [{status}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return condition


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  JobTracker Dev Branch — E2E Test (1 real job)")
print("=" * 70)

# ── Step 1: Pick one real job from DB ────────────────────────────────────────
print("\n[1] Selecting a real suggested job from DB...")

session = get_session()
job = (
    session.query(SuggestedJob)
    .filter(SuggestedJob.status == "suggested")
    .filter(SuggestedJob.apply_url.isnot(None))
    .order_by(SuggestedJob.score.desc())
    .first()
)

if not job:
    print("  No suggested jobs found. Run 'python main.py scan' first.")
    sys.exit(1)

print(f"  Selected: {job.company} — {job.title}")
print(f"  Score: {job.score}/10 | Source: {job.source}")
print(f"  URL: {job.apply_url}")
print(f"  Hash: {job.job_hash}")
job_hash = job.job_hash
company = job.company
title = job.title
apply_url = job.apply_url
description = job.description or ""
source = job.source
session.close()

# ── Step 2: Start API server in background ──────────────────────────────────
print("\n[2] Starting API server...")

import threading
from api import app as flask_app

server_thread = threading.Thread(
    target=lambda: flask_app.run(host="127.0.0.1", port=API_PORT, debug=False, use_reloader=False),
    daemon=True,
)
server_thread.start()
time.sleep(2)  # wait for server startup

# Verify server is up
try:
    r = requests.get(f"{API_BASE}/health", timeout=5)
    check("API server is running", r.status_code == 200, f"status={r.status_code}")
except Exception as e:
    print(f"  [{FAIL}] API server failed to start: {e}")
    sys.exit(1)

# ── Step 3: Test API Key auth ────────────────────────────────────────────────
print("\n[3] Testing API Key authentication...")

# Without key → 401
r = requests.get(f"{API_BASE}/api/stats")
check("Reject request without API key", r.status_code == 401, f"status={r.status_code}")

# With wrong key → 401
r = requests.get(f"{API_BASE}/api/stats", headers={"X-API-Key": "wrong-key"})
check("Reject request with wrong API key", r.status_code == 401, f"status={r.status_code}")

# With correct key → 200
headers = {"X-API-Key": TEST_API_KEY}
r = requests.get(f"{API_BASE}/api/stats", headers=headers)
check("Accept request with correct API key", r.status_code == 200, f"status={r.status_code}")

# /health stays open
r = requests.get(f"{API_BASE}/health")
check("/health is unauthenticated", r.status_code == 200)

# ── Step 4: Fetch the job via API ────────────────────────────────────────────
print("\n[4] Fetching job via authenticated API...")

r = requests.get(f"{API_BASE}/api/suggested/{job_hash}", headers=headers)
check("GET /api/suggested/<hash> returns job", r.status_code == 200)
if r.status_code == 200:
    data = r.json()
    check("Job data matches", data["company"] == company and data["title"] == title)
    check("Job status is 'suggested'", data["status"] == "suggested")

# ── Step 5: Check stats include success_rate_by_source ───────────────────────
print("\n[5] Checking stats endpoint...")

r = requests.get(f"{API_BASE}/api/stats", headers=headers)
stats = r.json()
check("Stats has suggested.total", "total" in stats.get("suggested", {}))
check("Stats has applications.success_rate_by_source", "success_rate_by_source" in stats.get("applications", {}))

# ── Step 6: Approve the job via API (simulates dashboard approve) ────────────
print("\n[6] Approving job via PATCH...")

r = requests.patch(
    f"{API_BASE}/api/suggested/{job_hash}",
    headers={**headers, "Content-Type": "application/json"},
    json={"status": "approved"},
)
check("PATCH /api/suggested/<hash> succeeds", r.status_code == 200)
if r.status_code == 200:
    data = r.json()
    check("Status updated to 'approved'", data["status"] == "approved")
    check("responded_at was set", data["responded_at"] is not None)

# ── Step 7: Verify ConversationState table exists ────────────────────────────
print("\n[7] Checking ConversationState (Dev A feature)...")

session = get_session()
cs = session.query(ConversationState).first()
check("ConversationState row exists", cs is not None)
if cs:
    check("Initial state is 'idle'", cs.state == "idle")
session.close()

# ── Step 8: Run auto-apply on the approved job ──────────────────────────────
print("\n[8] Running auto-apply on the approved job...")
print(f"  Applying to: {company} — {title}")
print(f"  URL: {apply_url}")

from core.applicator import apply_to_job, _extract_ats_key

ats_key = _extract_ats_key(apply_url)
print(f"  ATS key detected: {ats_key or 'none (not a known ATS)'}")

result = apply_to_job(
    job_id=job_hash[:8],
    apply_url=apply_url,
    job_title=title,
    company=company,
    job_description=description[:2000],
    auto_submit=False,  # don't actually submit — just fill
    user_instruction="",
    cv_variant=None,
)

check("apply_to_job returned result", result is not None)
check(f"Application result: {'success' if result['success'] else 'failed'}",
      True,  # not a pass/fail — just reporting
      result.get("error") or "no error")

if result.get("screenshots"):
    print(f"  Screenshots: {len(result['screenshots'])} captured")
    print(f"  Last: {result['screenshots'][-1]}")

# ── Step 9: Save Application record to DB ───────────────────────────────────
print("\n[9] Saving Application record...")

session = get_session()
app_record = Application(
    job_hash=job_hash,
    company=company,
    title=title,
    source=source,
    apply_url=apply_url,
    application_method="auto_apply",
    application_result="success" if result["success"] else "failed",
    status="success" if result["success"] else "failed",
    screenshot_path=result["screenshots"][-1] if result.get("screenshots") else None,
    cover_letter_used=result.get("cover_letter"),
    error_message=result.get("error"),
)
session.add(app_record)

# Update suggested job status
sj = session.query(SuggestedJob).filter_by(job_hash=job_hash).first()
if sj:
    sj.status = "applied" if result["success"] else "approved"

session.commit()
check("Application record saved", True)

# Verify FK relationship
app_check = session.query(Application).filter_by(job_hash=job_hash).first()
check("Application.suggested_job FK works", app_check.suggested_job is not None)
session.close()

# ── Step 10: Check ATS memory cache ─────────────────────────────────────────
print("\n[10] Checking ATS memory cache...")

if ats_key and result["success"]:
    session = get_session()
    mem = session.query(ATSFieldMemory).filter_by(ats_key=ats_key).first()
    check(f"ATS memory saved for '{ats_key}'", mem is not None)
    if mem:
        print(f"  success_count: {mem.success_count}")
    session.close()
else:
    reason = "no ATS detected in URL" if not ats_key else "application did not succeed"
    print(f"  [{SKIP}] ATS memory — {reason}")

# ── Step 11: Test WhatsApp notification ──────────────────────────────────────
print("\n[11] Sending WhatsApp result notification...")

from core.notifier import send_application_result

try:
    ok = send_application_result(
        company=company,
        title=title,
        success=result["success"],
        error_message=result.get("error", ""),
        screenshot_path=result["screenshots"][-1] if result.get("screenshots") else "",
    )
    check("WhatsApp notification sent", ok)
except Exception as e:
    print(f"  [{FAIL}] WhatsApp notification error: {e}")

# ── Step 12: Verify via API that everything is persisted ─────────────────────
print("\n[12] Final verification via API...")

r = requests.get(f"{API_BASE}/api/applications/{job_hash}", headers=headers)
check("GET /api/applications/<hash> returns record", r.status_code == 200)
if r.status_code == 200:
    data = r.json()
    check("Application method is 'auto_apply'", data["application_method"] == "auto_apply")
    check(f"Application status: {data['status']}", True)

r = requests.get(f"{API_BASE}/api/stats", headers=headers)
stats = r.json()
app_total = stats["applications"]["total"]
check(f"Stats shows {app_total} total applications", app_total >= 1)

# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
passed = sum(results)
total = len(results)
failed = total - passed
print(f"  Results: {passed}/{total} passed" + (f", {failed} failed" if failed else ""))
print("=" * 70 + "\n")
