"""
End-to-end test — scans both sources, filters last 24h, notifies via WhatsApp.

Run: python test_e2e.py
"""

import sys
import io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import asyncio
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

from scanners.hiremetech import scrape_hiremetech
from scanners.linkedin import scrape_linkedin
from core.analyzer import score_job, should_keep
from core.notifier import send_job_cards
from db.database import init_db, get_session
from db.models import Job

CUTOFF = datetime.now(timezone.utc) - timedelta(hours=24)


def is_within_24h(job: dict) -> bool:
    """Return True if job was posted within the last 24 hours."""
    date_str = job.get("date_posted")
    if not date_str:
        return False
    try:
        posted = datetime.fromisoformat(str(date_str)).replace(tzinfo=timezone.utc)
        return posted >= CUTOFF
    except (ValueError, TypeError):
        return False


async def run():
    init_db()

    with open(ROOT / "config" / "profile.yaml", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    sep = "=" * 60
    print(f"\n{sep}")
    print("JobTracker — End-to-End Test")
    print(f"24h cutoff: {CUTOFF.strftime('%d/%m/%Y %H:%M')} UTC")
    print(f"{sep}\n")

    # ── Hiremetech ─────────────────────────────────────────────────────────
    print("[ hiremetech.com ] Scraping all student/intern jobs...")
    all_hmt = await scrape_hiremetech(max_jobs=200)

    # Debug: show raw date values from API for first 10 jobs
    print("\n  [DEBUG] Raw date_posted from API (first 10 jobs):")
    for j in all_hmt[:10]:
        print(f"    {j['job_id']}  date_posted={repr(j.get('date_posted'))}  posted_at={j.get('posted_at')}")

    recent_hmt = [j for j in all_hmt if is_within_24h(j)]
    print(f"\n  Total scraped  : {len(all_hmt)}")
    print(f"  Posted last 24h: {len(recent_hmt)}")

    session = get_session()
    new_hmt = [
        j for j in recent_hmt
        if not session.query(Job).filter(Job.job_id == j["job_id"]).first()
    ]
    session.close()
    print(f"  New (not in DB): {len(new_hmt)}")

    hmt_passing = []
    hmt_scored = 0
    for job_data in new_hmt:
        try:
            result = await score_job(job_data, profile)
            hmt_scored += 1
        except Exception as e:
            print(f"  [WARN] Score failed {job_data['job_id']}: {e}")
            continue

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
                    salary=job_data.get("salary"),
                    score=result["score"],
                    role_type=result.get("role_type"),
                    tech_stack_match=result.get("tech_stack_match"),
                    is_student_position=int(result.get("is_student_position", False)),
                    apply_strategy=result.get("apply_strategy"),
                    status="notified" if should_keep(result) else "scored",
                )
                session.add(db_job)
                session.commit()
        except Exception as e:
            print(f"  [WARN] DB save failed {job_data['job_id']}: {e}")
            session.rollback()
        finally:
            session.close()

        if should_keep(result):
            hmt_passing.append({**job_data, **result})

    print(f"  Scored         : {hmt_scored}")
    print(f"  Passed filter  : {len(hmt_passing)}")

    # ── LinkedIn ────────────────────────────────────────────────────────────
    print("\n[ LinkedIn ] Scraping software intern jobs in Israel (last 24h)...")
    all_li = await scrape_linkedin(max_jobs=40)
    print(f"  Total scraped  : {len(all_li)}")

    session = get_session()
    new_li = [
        j for j in all_li
        if not session.query(Job).filter(Job.job_id == j["job_id"]).first()
    ]
    session.close()
    print(f"  New (not in DB): {len(new_li)}")

    li_passing = []
    li_scored = 0
    for job_data in new_li:
        try:
            result = await score_job(job_data, profile)
            li_scored += 1
        except Exception as e:
            print(f"  [WARN] Score failed {job_data['job_id']}: {e}")
            continue

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
                    status="notified" if should_keep(result) else "scored",
                )
                session.add(db_job)
                session.commit()
        except Exception as e:
            print(f"  [WARN] DB save failed {job_data['job_id']}: {e}")
            session.rollback()
        finally:
            session.close()

        if should_keep(result):
            li_passing.append({**job_data, **result})

    print(f"  Scored         : {li_scored}")
    print(f"  Passed filter  : {len(li_passing)}")

    # ── Referally WhatsApp bridge stats ────────────────────────────────────
    print("\n[ Referally WhatsApp bridge ] (processed by whatsapp_bridge.py)")
    session = get_session()
    wa_jobs     = session.query(Job).filter(Job.job_id.like("WA-%")).order_by(Job.score.desc()).all()
    wa_total    = len(wa_jobs)
    wa_notified = sum(1 for j in wa_jobs if j.status == "notified")
    session.close()
    print(f"  Total WA jobs in DB      : {wa_total}")
    print(f"  Passed filter (notified) : {wa_notified}")
    if wa_jobs:
        print("  [DEBUG] All Referally jobs (score / level / title):")
        for j in wa_jobs:
            mark = "✓ SENT" if j.status == "notified" else "✗ skip"
            print(f"    {mark}  score={j.score}  level={j.role_type or '?'}  {j.title[:50]} @ {j.company}")

    # ── Send WhatsApp notifications ────────────────────────────────────────
    print("\n[ Sending WhatsApp notifications ]")
    hmt_sent = hmt_failed = 0
    if hmt_passing:
        hmt_sent, hmt_failed = send_job_cards(hmt_passing)
        print(f"  Hiremetech: {hmt_sent} sent, {hmt_failed} failed")
    else:
        print("  Hiremetech: no new matching jobs to send")

    li_sent = li_failed = 0
    if li_passing:
        li_sent, li_failed = send_job_cards(li_passing)
        print(f"  LinkedIn:   {li_sent} sent, {li_failed} failed")
    else:
        print("  LinkedIn:   no new matching jobs to send")

    # ── Summary ────────────────────────────────────────────────────────────
    total_sent = hmt_sent + li_sent
    print(f"\n{sep}")
    print("SUMMARY")
    print(f"{sep}")
    print(f"  Hiremetech — found last 24h        : {len(recent_hmt)}")
    print(f"  Hiremetech — passed student/junior  : {len(hmt_passing)}")
    print(f"  LinkedIn   — scraped               : {len(all_li)}")
    print(f"  LinkedIn   — passed student/junior  : {len(li_passing)}")
    print(f"  Referally WA — processed via bridge : {wa_total}")
    print(f"  Referally WA — passed student/junior: {wa_notified}")
    print(f"  WhatsApp messages sent this run     : {total_sent}")
    print(f"{sep}\n")


asyncio.run(run())
