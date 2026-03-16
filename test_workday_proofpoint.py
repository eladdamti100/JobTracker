"""
WorkdayAdapter — End-to-end verification: Proofpoint Data Engineer Intern
Branch: adapters-il  |  Step A0.1

Run:
    python test_workday_proofpoint.py

What it does:
  1. Inserts the Proofpoint Workday job into the DB as status="approved"
     (skips insert if the job_hash already exists).
  2. Runs the full ApplyOrchestrator with auto_submit=True.
  3. Prints the final result and screenshot path.

Expected path (happy):
  PLAN → RESTORE_SESSION / LOGIN → FILL_FORM → REVIEW → SUBMIT → SUCCESS

Notes:
  - Browser opens in non-headless mode (slow_mo=150) so you can watch.
  - If a Workday account already exists: adapter loads saved session.
  - If no account: adapter signs up automatically (needs GMAIL_ADDRESS in .env).
  - On CAPTCHA/MFA: adapter pauses to HUMAN_INTERVENTION and
    sends you a WhatsApp message — reply DONE after handling it.
"""

import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# ── Setup ─────────────────────────────────────────────────────────────────────

from db.database import init_db, get_session
from db.models import SuggestedJob, make_job_hash

COMPANY     = "Proofpoint"
TITLE       = "Data Engineer (Detection) Intern"
APPLY_URL   = (
    "https://proofpoint.wd5.myworkdayjobs.com/en-US/proofpointcareers/job"
    "/Tel-Aviv-Israel/Detection-Engineer-Intern_R13806"
)
DESCRIPTION = (
    "Hands-on experience using Python, Pandas and big data querying "
    "(AWS Athena preferred) for analyzing and parsing large data sets. "
    "Strong investigative skills to troubleshoot false positives/negatives "
    "and enhance detection accuracy. Work with cross-functional teams including "
    "threat intelligence, product management, and engineering."
)

SEP = "=" * 65


def get_or_create_job(session) -> SuggestedJob:
    job_hash = make_job_hash(COMPANY, TITLE, APPLY_URL)
    existing = session.query(SuggestedJob).filter_by(job_hash=job_hash).first()
    if existing:
        print(f"  [DB] Job already in DB: {existing!r}")
        if existing.status != "approved":
            print(f"  [DB] Updating status: {existing.status!r} → 'approved'")
            existing.status = "approved"
            session.commit()
        return existing

    print(f"  [DB] Inserting new job (hash={job_hash[:8]})")
    job = SuggestedJob(
        job_hash    = job_hash,
        company     = COMPANY,
        title       = TITLE,
        source      = "Manual",
        apply_url   = APPLY_URL,
        location    = "Tel Aviv, Israel",
        description = DESCRIPTION,
        score       = 8.5,
        level       = "student",
        status      = "approved",
    )
    session.add(job)
    session.commit()
    return job


def main():
    init_db()

    print(f"\n{SEP}")
    print("WorkdayAdapter — End-to-End Test")
    print(f"Job : {TITLE} @ {COMPANY}")
    print(f"URL : {APPLY_URL}")
    print(f"{SEP}\n")

    session = get_session()
    try:
        job = get_or_create_job(session)
    finally:
        session.close()

    # Re-fetch in a fresh session so the orchestrator owns its own session
    session2 = get_session()
    try:
        job_hash = make_job_hash(COMPANY, TITLE, APPLY_URL)
        job = session2.query(SuggestedJob).filter_by(job_hash=job_hash).first()
    finally:
        session2.close()

    print(f"  Running ApplyOrchestrator (auto_submit=True)...\n")

    from core.orchestrator import run_application

    result = run_application(job, auto_submit=True)

    print(f"\n{SEP}")
    print("RESULT")
    print(f"{SEP}")
    print(f"  success       : {result.success}")
    print(f"  final_state   : {result.final_state.value}")
    print(f"  adapter       : {result.adapter_name}")
    print(f"  steps_taken   : {result.steps_taken}")
    if result.error:
        print(f"  error         : {result.error}")
    if result.screenshot_path:
        print(f"  screenshot    : {result.screenshot_path}")
    print(f"{SEP}\n")

    if result.success:
        print("PASS — Application submitted successfully via WorkdayAdapter")
    elif result.final_state.value == "human_intervention":
        print("PAUSED — Waiting for human input (check WhatsApp)")
    else:
        print(f"FAIL — {result.error}")

    return result.success


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
