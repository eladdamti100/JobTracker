"""Quick test: scan 2 real jobs, score them with Groq, send via WhatsApp.

After running this, reply YES or NO in WhatsApp to test the full flow.
The webhook server must be running: python main.py webhook

Usage:
  python test_scan_2jobs.py
"""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from db.database import init_db, get_session, is_duplicate
from db.models import SuggestedJob, make_job_hash
from scanners.hiremetech import scrape_hiremetech
from core.analyzer import score_job, should_keep
from core.notifier import send_suggestion

init_db()

TARGET_COUNT = 2  # how many jobs to send via WhatsApp


async def main():
    with open(Path(__file__).parent / "config" / "profile.yaml", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    print("Scraping HireMeTech...")
    all_jobs = await scrape_hiremetech(max_jobs=50)
    print(f"  Found {len(all_jobs)} jobs total")

    session = get_session()
    sent = 0

    for job_data in all_jobs:
        if sent >= TARGET_COUNT:
            break

        job_hash = make_job_hash(
            job_data.get("company", ""),
            job_data.get("title", ""),
            job_data.get("apply_url", ""),
        )

        if is_duplicate(job_hash):
            continue

        print(f"\nScoring: {job_data.get('company')} — {job_data.get('title')}...")
        try:
            result = await score_job(job_data, profile)
        except Exception as e:
            print(f"  Score failed: {e}")
            continue

        print(f"  Score: {result['score']}/10 | Level: {result.get('level')} | Type: {result.get('role_type')}")

        if not should_keep(result):
            print(f"  Below threshold — skipping")
            continue

        # Save to DB
        suggested = SuggestedJob(
            job_hash=job_hash,
            company=job_data["company"],
            title=job_data["title"],
            source="HireMeTech",
            apply_url=job_data.get("apply_url"),
            location=job_data.get("location"),
            description=job_data.get("description"),
            date_posted=job_data.get("date_posted"),
            salary=job_data.get("salary"),
            score=result["score"],
            reason=result.get("reason"),
            level=result.get("level"),
            role_type=result.get("role_type"),
            tech_stack_match=result.get("tech_stack_match"),
            is_student_position=int(result.get("is_student_position", False)),
            apply_strategy=result.get("apply_strategy"),
            role_summary=result.get("role_summary"),
            requirements_summary=result.get("requirements_summary"),
            status="suggested",
        )
        session.add(suggested)
        session.flush()

        # Send WhatsApp card
        job_card = {**job_data, **result, "job_hash": job_hash}
        ok = send_suggestion(job_card)
        if ok:
            sent += 1
            print(f"  ✅ Sent to WhatsApp ({sent}/{TARGET_COUNT})")
        else:
            print(f"  ❌ WhatsApp send failed")

    session.commit()
    session.close()

    print(f"\n{'='*50}")
    print(f"Done! Sent {sent} jobs to WhatsApp.")
    if sent > 0:
        print(f"\nNow test the flow:")
        print(f"  1. Reply YES to the first job → then reply 'כן' to apply")
        print(f"  2. Reply NO to the second job → it should be rejected")
        print(f"\nMake sure the webhook is running: python main.py webhook")
    print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
