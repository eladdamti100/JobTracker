"""
LinkedIn scanner test — scrape, score with Claude, send passing jobs to WhatsApp.

Run: python test_linkedin.py
"""

import sys
import io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import asyncio
import yaml
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

from scanners.linkedin import scrape_linkedin
from core.analyzer import score_job, should_keep
from core.notifier import send_job_cards
from db.database import init_db, get_session
from db.models import Job

sep = "=" * 60


async def run():
    init_db()

    with open(ROOT / "config" / "profile.yaml", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    print(f"\n{sep}")
    print("LinkedIn Scanner — Scrape + Score + Notify")
    print(f"{sep}\n")

    # ── Scrape ──────────────────────────────────────────────────────────────
    print("Scraping LinkedIn (software intern, Israel, last 24h)...")
    all_jobs = await scrape_linkedin(max_jobs=40)
    print(f"  Scraped        : {len(all_jobs)}")

    session = get_session()
    new_jobs = [
        j for j in all_jobs
        if not session.query(Job).filter(Job.job_id == j["job_id"]).first()
    ]
    session.close()
    print(f"  New (not in DB): {len(new_jobs)}")

    # ── Score ────────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("Scoring with Claude...\n")

    passing = []
    scored = 0

    for job_data in new_jobs:
        try:
            result = await score_job(job_data, profile)
            scored += 1
        except Exception as e:
            print(f"  [WARN] Score failed {job_data['job_id']}: {e}")
            continue

        keep = should_keep(result)
        mark = "✓ PASS" if keep else "✗ skip"
        print(
            f"  {mark}  score={result['score']:2}/10  "
            f"level={result.get('level','?'):<7}  "
            f"type={result.get('role_type','?'):<12}  "
            f"{job_data['title'][:40]:<40}  @  {job_data['company']}"
        )

        # Save to DB
        session = get_session()
        try:
            if not session.query(Job).filter(Job.job_id == job_data["job_id"]).first():
                db_job = Job(
                    job_id=job_data["job_id"],
                    title=job_data["title"],
                    company=job_data["company"],
                    location=job_data.get("location"),
                    description=job_data.get("description"),
                    apply_url=job_data.get("apply_url"),
                    date_posted=job_data.get("date_posted"),
                    salary=None,
                    score=result["score"],
                    level=result.get("level"),
                    role_type=result.get("role_type"),
                    tech_stack_match=result.get("tech_stack_match"),
                    is_student_position=int(result.get("is_student_position", False)),
                    apply_strategy=result.get("apply_strategy"),
                    role_summary=result.get("role_summary"),
                    requirements_summary=result.get("requirements_summary"),
                    status="notified" if keep else "scored",
                )
                session.add(db_job)
                session.commit()
        except Exception as e:
            print(f"  [WARN] DB save failed {job_data['job_id']}: {e}")
            session.rollback()
        finally:
            session.close()

        if keep:
            passing.append({**job_data, **result})

    # ── Notify ───────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("SUMMARY")
    print(f"{sep}")
    print(f"  Scraped  : {len(all_jobs)}")
    print(f"  New      : {len(new_jobs)}")
    print(f"  Scored   : {scored}")
    print(f"  Passing  : {len(passing)}")

    if passing:
        print(f"\nSending {len(passing)} jobs to WhatsApp...")
        sent, failed = send_job_cards(passing)
        print(f"  Sent: {sent}  Failed: {failed}")
    else:
        print("\nNo passing jobs to send.")
    print(sep)


asyncio.run(run())
