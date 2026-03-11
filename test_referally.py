"""
Referally-only test:
  1. Reset DB (add new columns if missing, clear WA- jobs)
  2. Start bridge inline (no separate terminal needed)
  3. Wait for Node.js catch-up to process URLs
  4. Print all results + send matching jobs to WhatsApp

Run: python test_referally.py
Make sure Node.js is running: node scanners/whatsapp_group.js
"""

import sys
import io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import os
import time
import threading
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

from db.database import init_db, get_session, get_engine
from db.models import Job, Base
from core.analyzer import should_keep
from core.notifier import send_job_cards

sep = "=" * 60


def step1_reset_db():
    """Add new columns if needed, then clear all WA- jobs."""
    from sqlalchemy import inspect, text

    init_db()  # creates tables if not exist

    # Add missing columns (level, role_summary, requirements_summary)
    engine = get_engine()
    inspector = inspect(engine)
    existing = {col["name"] for col in inspector.get_columns("jobs")}
    with engine.connect() as conn:
        for col_name, col_type in [
            ("level", "VARCHAR"),
            ("role_summary", "TEXT"),
            ("requirements_summary", "TEXT"),
        ]:
            if col_name not in existing:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}"))
                print(f"  Added column: {col_name}")
        conn.commit()

    session = get_session()
    deleted = session.query(Job).filter(Job.job_id.like("WA-%")).delete()
    session.commit()
    session.close()
    print(f"  Cleared {deleted} old Referally (WA-) jobs from DB")


def step2_start_bridge():
    """Start the WhatsApp bridge server in a background thread."""
    from scanners.whatsapp_bridge import app
    port = int(os.environ.get("BRIDGE_PORT", 5001))

    def run_bridge():
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=run_bridge, daemon=True)
    t.start()
    time.sleep(1)
    print(f"  Bridge running on port {port}")


def step3_wait_for_node(timeout_sec=300):
    """Poll DB for new WA- jobs until count stabilizes (no new jobs for 30s)."""
    print("  Waiting for Node.js to forward URLs and bridge to process them...")
    print("  (Make sure 'node scanners/whatsapp_group.js' is running)")
    print()

    last_count = 0
    stable_since = time.time()
    start = time.time()

    while time.time() - start < timeout_sec:
        session = get_session()
        count = session.query(Job).filter(Job.job_id.like("WA-%")).count()
        session.close()

        if count != last_count:
            print(f"  ... {count} jobs processed so far")
            last_count = count
            stable_since = time.time()
        elif count > 0 and time.time() - stable_since > 30:
            break

        time.sleep(3)

    return last_count


def step4_show_results_and_notify():
    """Print all WA jobs from DB, send matching ones to WhatsApp."""
    session = get_session()
    wa_jobs = session.query(Job).filter(Job.job_id.like("WA-%")).order_by(Job.score.desc()).all()
    session.close()

    total = len(wa_jobs)
    print(f"\n{sep}")
    print(f"REFERALLY RESULTS — {total} jobs processed")
    print(f"{sep}\n")

    passing = []
    for j in wa_jobs:
        level = j.level or ("student" if j.is_student_position else "unknown")
        result = {"score": j.score or 0, "level": level}
        keeps = should_keep(result)

        mark = "PASS" if keeps else "SKIP"
        score_str = f"{j.score:.0f}" if j.score else "?"
        print(f"  [{mark}] score={score_str:>2}  level={level:<8}  type={j.role_type or '?':<12}")
        print(f"         {j.title[:60]}")
        print(f"         {j.company} | {j.apply_url}")
        print()

        if keeps:
            passing.append({
                "job_id": j.job_id,
                "title": j.title,
                "company": j.company,
                "location": j.location or "ישראל",
                "apply_url": j.apply_url,
                "posted_at": j.date_posted or "N/A",
                "score": j.score,
                "level": level,
                "role_type": j.role_type,
                "role_summary": j.role_summary or "",
                "requirements_summary": j.requirements_summary or "",
            })

    print(f"{sep}")
    print(f"  Total processed : {total}")
    print(f"  Passed filter   : {len(passing)}")
    print(f"{sep}\n")

    if passing:
        print("Sending matching jobs to WhatsApp...")
        sent, failed = send_job_cards(passing)
        print(f"  Sent: {sent}, Failed: {failed}")
    else:
        print("No matching jobs to send.")

    return total, len(passing)


def main():
    print(f"\n{sep}")
    print("JobTracker — Referally WhatsApp Group Test")
    print(f"{sep}\n")

    print("[STEP 1] Reset DB for fresh Referally scan")
    step1_reset_db()

    print("\n[STEP 2] Start bridge server")
    step2_start_bridge()

    print("\n[STEP 3] Wait for Node.js catch-up")
    print("  >>> Start Node.js now: node scanners/whatsapp_group.js")
    count = step3_wait_for_node()
    print(f"\n  Done: {count} jobs processed")

    print("\n[STEP 4] Results & WhatsApp notifications")
    step4_show_results_and_notify()


if __name__ == "__main__":
    main()
