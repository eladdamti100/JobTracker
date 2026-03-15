"""
Inbound WhatsApp webhook via Flask + Twilio.

Human-in-the-loop flow:
  YES    → approve last suggested job, trigger auto-apply
  NO     → reject last suggested job permanently
  SKIP   → snooze for 12 hours
  STATUS → show stats summary
  SCAN   → trigger immediate scan

Run standalone:  python webhook.py
Or via main.py:  python main.py webhook
"""

import os
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

app = Flask(__name__)


def _run_async(coro):
    """Run an async coroutine from a sync Flask handler."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get_last_suggested():
    """Get the most recent suggested job that's still pending."""
    from db.database import get_session
    from db.models import SuggestedJob

    session = get_session()
    try:
        job = (
            session.query(SuggestedJob)
            .filter(SuggestedJob.status == "suggested")
            .order_by(SuggestedJob.created_at.desc())
            .first()
        )
        if job:
            # Expunge so we can use it after session closes
            from sqlalchemy.orm import make_transient
            session.expunge(job)
        return job
    finally:
        session.close()


def _handle_yes() -> str:
    """Approve the last suggested job and trigger auto-apply."""
    from db.database import get_session
    from db.models import SuggestedJob, Application

    session = get_session()
    try:
        job = (
            session.query(SuggestedJob)
            .filter(SuggestedJob.status == "suggested")
            .order_by(SuggestedJob.created_at.desc())
            .first()
        )
        if not job:
            return "❌ אין משרות ממתינות לאישור."

        job.status = "approved"
        job.responded_at = datetime.now(timezone.utc)
        session.commit()

        company = job.company
        title = job.title
        job_hash = job.job_hash
        apply_url = job.apply_url
        description = job.description or ""

        # Trigger auto-apply in background thread
        def run_apply():
            from db.database import get_session as gs
            from db.models import SuggestedJob as SJ, Application as App
            from core.applicator import apply_to_job
            from core.notifier import send_whatsapp

            result = apply_to_job(
                job_id=job_hash[:8],
                apply_url=apply_url,
                job_title=title,
                company=company,
                job_description=description,
                auto_submit=True,
            )

            s = gs()
            try:
                # Update suggested job status
                sj = s.query(SJ).filter_by(job_hash=job_hash).first()
                if sj:
                    sj.status = "applied"

                now = datetime.now(timezone.utc)
                app_record = App(
                    job_hash=job_hash,
                    company=company,
                    title=title,
                    apply_url=apply_url,
                    applied_at=now,
                    application_method="auto_apply",
                )

                if result["success"]:
                    app_record.status = "success"
                    app_record.application_result = result.get("application_result", "success")
                    app_record.cover_letter_used = result.get("cover_letter")
                    screenshots = result.get("screenshots", [])
                    app_record.screenshot_path = screenshots[0] if screenshots else None
                    send_whatsapp(f"✅ הוגש בהצלחה! {company} — {title}")
                else:
                    app_record.status = "failed"
                    app_record.application_result = "failed"
                    app_record.error_message = result.get("error", "Unknown error")
                    send_whatsapp(f"❌ ההגשה נכשלה: {company} — {title}\n{result.get('error', '')}")

                s.add(app_record)
                s.commit()
            except Exception as e:
                logger.error(f"Apply thread error: {e}")
                s.rollback()
            finally:
                s.close()

        thread = threading.Thread(target=run_apply, daemon=True)
        thread.start()

        return f"✅ מגיש ל-{company} עכשיו...\nאעדכן כשזה יסתיים."

    finally:
        session.close()


def _handle_no() -> str:
    """Reject the last suggested job permanently."""
    from db.database import get_session
    from db.models import SuggestedJob

    session = get_session()
    try:
        job = (
            session.query(SuggestedJob)
            .filter(SuggestedJob.status == "suggested")
            .order_by(SuggestedJob.created_at.desc())
            .first()
        )
        if not job:
            return "❌ אין משרות ממתינות."

        job.status = "rejected"
        job.responded_at = datetime.now(timezone.utc)
        session.commit()

        return f"❌ הבנתי, מדלג על {job.company}."
    finally:
        session.close()


def _handle_skip() -> str:
    """Snooze the last suggested job — re-suggest in 12 hours."""
    from db.database import get_session
    from db.models import SuggestedJob

    session = get_session()
    try:
        job = (
            session.query(SuggestedJob)
            .filter(SuggestedJob.status == "suggested")
            .order_by(SuggestedJob.created_at.desc())
            .first()
        )
        if not job:
            return "❌ אין משרות ממתינות."

        job.status = "skipped"
        job.responded_at = datetime.now(timezone.utc)
        job.expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
        session.commit()

        return f"⏰ אזכיר לך על {job.company} בעוד 12 שעות."
    finally:
        session.close()


def _handle_status() -> str:
    """Return stats summary."""
    from db.database import get_session
    from db.models import SuggestedJob, Application
    from sqlalchemy import func

    session = get_session()
    try:
        pending = session.query(SuggestedJob).filter(SuggestedJob.status == "suggested").count()
        applied = session.query(Application).filter(Application.status == "success").count()
        rejected = session.query(SuggestedJob).filter(SuggestedJob.status == "rejected").count()
        failed = session.query(Application).filter(Application.status == "failed").count()

        # Last scan time — most recent suggested job
        last = (
            session.query(SuggestedJob)
            .order_by(SuggestedJob.created_at.desc())
            .first()
        )
        last_scan = last.created_at.strftime("%d/%m %H:%M") if last and last.created_at else "לא ידוע"

        return (
            f"📊 *JobTracker Status*\n\n"
            f"🆕 ממתינות לתשובה: {pending}\n"
            f"✅ הוגשו: {applied}\n"
            f"❌ נדחו: {rejected}\n"
            f"💥 נכשלו: {failed}\n"
            f"📅 סריקה אחרונה: {last_scan}"
        )
    finally:
        session.close()


def _handle_scan() -> str:
    """Trigger a scan in a background thread."""
    def run_scan():
        import yaml
        from pathlib import Path
        from scanners.hiremetech import scrape_hiremetech
        from core.analyzer import score_job, should_keep
        from core.notifier import send_suggestion, send_whatsapp
        from db.database import get_session, init_db, is_duplicate
        from db.models import SuggestedJob, make_job_hash

        init_db()

        with open(Path(__file__).parent / "config" / "profile.yaml", encoding="utf-8") as f:
            profile = yaml.safe_load(f)

        jobs = _run_async(scrape_hiremetech(max_jobs=100))
        session = get_session()
        new_count = 0

        for job_data in jobs:
            job_hash = make_job_hash(
                job_data.get("company", ""),
                job_data.get("title", ""),
                job_data.get("apply_url", ""),
            )

            if is_duplicate(job_hash):
                continue

            try:
                result = _run_async(score_job(job_data, profile))
            except Exception as e:
                logger.error(f"Score failed {job_data.get('job_id', '?')}: {e}")
                continue

            if not should_keep(result):
                continue

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
            new_count += 1

            send_suggestion({**job_data, **result})

        session.commit()
        session.close()

        if new_count == 0:
            send_whatsapp("🔍 סריקה הושלמה — אין התאמות חדשות.")
        else:
            logger.success(f"Scan complete: {new_count} new suggestions sent")

    thread = threading.Thread(target=run_scan, daemon=True)
    thread.start()
    return "🔍 סריקה התחילה! תקבל עדכון כשמסתיים..."


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming = (request.form.get("Body") or "").strip()
    sender = request.form.get("From") or ""
    logger.info(f"Incoming WhatsApp from {sender}: {incoming!r}")

    resp = MessagingResponse()
    upper = incoming.upper().strip()

    if upper in ("YES", "כן", "Y"):
        reply = _handle_yes()

    elif upper in ("NO", "לא", "N"):
        reply = _handle_no()

    elif upper in ("SKIP", "דלג", "S"):
        reply = _handle_skip()

    elif upper == "STATUS":
        reply = _handle_status()

    elif upper == "SCAN":
        reply = _handle_scan()

    elif upper in ("HELP", "עזרה", "?"):
        reply = (
            "🤖 *JobTracker — פקודות:*\n\n"
            "✅ *YES* — אשר והגש למשרה האחרונה\n"
            "❌ *NO* — דלג על המשרה האחרונה\n"
            "⏰ *SKIP* — הזכר לי בעוד 12 שעות\n"
            "📊 *STATUS* — הצג סטטיסטיקות\n"
            "🔍 *SCAN* — סרוק משרות חדשות עכשיו"
        )
    else:
        reply = (
            "לא הבנתי 🤔\n"
            "שלח HELP לרשימת הפקודות."
        )

    resp.message(reply)
    return str(resp), 200, {"Content-Type": "text/xml"}


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "JobTracker webhook"}, 200


if __name__ == "__main__":
    from db.database import init_db
    init_db()
    port = int(os.environ.get("WEBHOOK_PORT", 5000))
    logger.info(f"Starting webhook server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
