"""APScheduler setup for JobTracker autonomous operation.

Jobs:
  scan_job      — every 6 hours: scrape HireMeTech + LinkedIn, score, suggest
  expiry_job    — every 1 hour:  expire stale suggestions, re-notify skipped jobs
  renotify_job  — runs inside expiry_job (not a separate schedule)

Design choice — 6h vs 12h:
  HireMeTech and LinkedIn are polled (no push API exists). 6h gives better
  coverage while staying polite to both sites. Smart cooldown: if we found
  ≥1 new job in the last 2h we skip the scan to avoid hammering.
"""

import asyncio
import threading
from datetime import datetime, timezone, timedelta
from loguru import logger
from apscheduler.schedulers.background import BackgroundScheduler


# ── Scan ──────────────────────────────────────────────────────────────────────

def run_scan(force: bool = False) -> int:
    """Run a full scan (HireMeTech + LinkedIn). Returns number of new suggestions."""
    from db.database import get_session, is_duplicate, init_db
    from db.models import SuggestedJob, make_job_hash
    from scanners.hiremetech import scrape_hiremetech
    from scanners.linkedin import scrape_linkedin
    from core.analyzer import score_job, should_keep
    from core.notifier import send_suggestion, send_whatsapp
    import yaml
    from pathlib import Path

    # Smart cooldown: skip if a job was suggested in the last 2h
    if not force:
        s = get_session()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
            recent = (
                s.query(SuggestedJob)
                .filter(SuggestedJob.created_at >= cutoff)
                .first()
            )
            if recent:
                logger.info(
                    f"Scan cooldown: found job suggested {recent.created_at} — skipping this cycle"
                )
                return 0
        finally:
            s.close()

    init_db()

    profile_path = Path(__file__).parent.parent / "config" / "profile.yaml"
    with open(profile_path, encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    loop = asyncio.new_event_loop()
    try:
        hmt_jobs = loop.run_until_complete(scrape_hiremetech(max_jobs=200))
        li_jobs = loop.run_until_complete(scrape_linkedin(max_jobs=40))
    finally:
        loop.close()

    # Filter HireMeTech to last 24h
    cutoff24 = datetime.now(timezone.utc) - timedelta(hours=24)
    def is_recent(job):
        d = job.get("date_posted")
        if not d:
            return False
        try:
            return datetime.fromisoformat(str(d)).replace(tzinfo=timezone.utc) >= cutoff24
        except (ValueError, TypeError):
            return False

    jobs = [j for j in hmt_jobs if is_recent(j)] + li_jobs
    logger.info(f"Scan: {len(jobs)} candidate jobs to score")

    session = get_session()
    new_count = 0
    try:
        for job_data in jobs:
            job_hash = make_job_hash(
                job_data.get("company", ""),
                job_data.get("title", ""),
                job_data.get("apply_url", ""),
            )
            if is_duplicate(job_hash):
                continue

            try:
                loop2 = asyncio.new_event_loop()
                result = loop2.run_until_complete(score_job(job_data, profile))
                loop2.close()
            except Exception as e:
                logger.error(f"Score failed {job_data.get('job_id', '?')}: {e}")
                continue

            if not should_keep(result):
                continue

            source = "LinkedIn" if job_data.get("job_id", "").startswith("LI-") else "HireMeTech"
            suggested = SuggestedJob(
                job_hash=job_hash,
                company=job_data["company"],
                title=job_data["title"],
                source=source,
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
            new_count += 1
            send_suggestion({**job_data, **result})

        session.commit()
    except Exception as e:
        logger.error(f"Scan error: {e}")
        session.rollback()
    finally:
        session.close()

    if new_count == 0:
        logger.info("Scheduled scan complete — no new matching jobs")
    else:
        logger.success(f"Scheduled scan complete: {new_count} new suggestions sent")

    return new_count


# ── Expiry + re-notification ───────────────────────────────────────────────────

def run_expiry_and_renotify() -> tuple[int, int]:
    """Expire stale suggestions and re-notify skipped jobs whose snooze has elapsed.

    Returns (expired_count, renotified_count).
    """
    from db.database import get_session
    from db.models import SuggestedJob
    from core.notifier import send_suggestion, send_whatsapp

    session = get_session()
    expired_count = 0
    renotified_count = 0
    now = datetime.now(timezone.utc)

    try:
        # ── 1. Expire overdue suggested jobs ─────────────────────────────
        overdue = (
            session.query(SuggestedJob)
            .filter(
                SuggestedJob.status == "suggested",
                SuggestedJob.expires_at < now,
            )
            .all()
        )
        for job in overdue:
            job.status = "expired"
            logger.info(f"Expired: {job.company} — {job.title}")
            expired_count += 1

        # ── 2. Re-notify skipped jobs whose snooze has elapsed ────────────
        # Skipped jobs have expires_at set to +12h from when SKIP was sent.
        snoozed = (
            session.query(SuggestedJob)
            .filter(
                SuggestedJob.status == "skipped",
                SuggestedJob.expires_at < now,
            )
            .all()
        )
        for job in snoozed:
            # Reset to suggested so the user can respond again
            job.status = "suggested"
            job.expires_at = now + timedelta(hours=24)
            job.responded_at = None

            job_data = {
                "company": job.company,
                "title": job.title,
                "apply_url": job.apply_url,
                "location": job.location,
                "description": job.description,
                "score": job.score,
                "reason": job.reason,
                "level": job.level,
                "role_type": job.role_type,
                "tech_stack_match": job.tech_stack_match,
                "is_student_position": bool(job.is_student_position),
                "apply_strategy": job.apply_strategy,
                "role_summary": job.role_summary,
                "requirements_summary": job.requirements_summary,
            }
            send_suggestion(job_data)
            logger.info(f"Re-notified skipped job: {job.company} — {job.title}")
            renotified_count += 1

        if expired_count or renotified_count:
            session.commit()

        if expired_count:
            logger.info(f"Expiry check: {expired_count} expired, {renotified_count} re-notified")

        return expired_count, renotified_count

    except Exception as e:
        logger.error(f"Expiry/renotify error: {e}")
        session.rollback()
        return 0, 0
    finally:
        session.close()


# ── Scheduler bootstrap ────────────────────────────────────────────────────────

_scheduler: BackgroundScheduler | None = None


def start_scheduler(scan_interval_hours: int = 6) -> BackgroundScheduler:
    """Start the APScheduler background scheduler.

    Returns the scheduler instance so callers can shut it down if needed.
    """
    global _scheduler

    if _scheduler and _scheduler.running:
        logger.warning("Scheduler already running — returning existing instance")
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC")

    # Scan every N hours (default 6)
    _scheduler.add_job(
        run_scan,
        trigger="interval",
        hours=scan_interval_hours,
        id="scan_job",
        max_instances=1,
        coalesce=True,
    )

    # Expiry + re-notification every hour
    _scheduler.add_job(
        run_expiry_and_renotify,
        trigger="interval",
        hours=1,
        id="expiry_job",
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()
    logger.success(
        f"Scheduler started — scan every {scan_interval_hours}h, expiry/re-notify every 1h"
    )

    # Run an immediate scan in a background thread so startup isn't blocked
    threading.Thread(target=run_scan, kwargs={"force": True}, daemon=True).start()
    logger.info("Initial scan triggered in background thread")

    return _scheduler


def stop_scheduler():
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
