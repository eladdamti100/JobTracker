"""
Inbound WhatsApp webhook via Flask + Twilio.

Supported commands (send from WhatsApp):
  APPLY HMT-{id}   → trigger auto-apply for that job
  STATUS           → reply with stats
  SCAN             → trigger immediate scan now

Run standalone:  python webhook.py
Or via main.py:  python main.py webhook
"""

import os
import asyncio
import threading
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


def _handle_apply(job_id: str) -> str:
    """Handle APPLY command — stub until applicator is implemented."""
    from db.database import get_session
    from db.models import Job

    session = get_session()
    try:
        job = session.query(Job).filter(Job.job_id == job_id).first()
        if not job:
            return f"❌ לא נמצאה משרה עם מזהה {job_id}"
        if job.status == "applied":
            return f"✅ כבר הוגשת ל-{job.company} ({job_id})"
        # Mark as approved so the applicator can pick it up
        job.status = "approved"
        session.commit()
        return (
            f"✅ משרה {job_id} סומנה להגשה!\n"
            f"🏢 {job.company} — {job.title}\n"
            f"הגשה אוטומטית תרוץ בקרוב..."
        )
    finally:
        session.close()


def _handle_status() -> str:
    from db.database import get_session
    from db.models import Job
    from sqlalchemy import func

    session = get_session()
    try:
        total = session.query(Job).count()
        by_status = dict(
            session.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
        )
        lines = ["📊 *JobTracker Status*\n"]
        lines.append(f"סה״כ משרות שנמצאו: {total}")
        status_labels = {
            "new": "חדשות",
            "scored": "עברו ניקוד",
            "notified": "נשלחה התראה",
            "approved": "מאושרות להגשה",
            "applying": "בתהליך הגשה",
            "applied": "הוגשו ✅",
            "failed": "נכשלו ❌",
        }
        for s, count in by_status.items():
            label = status_labels.get(s, s)
            lines.append(f"  {label}: {count}")
        return "\n".join(lines)
    finally:
        session.close()


def _handle_scan() -> str:
    """Trigger a scan in a background thread and return immediate acknowledgment."""
    def run_scan():
        import yaml
        from pathlib import Path
        from scanners.hiremetech import scrape_hiremetech
        from core.analyzer import score_job, should_keep
        from core.notifier import send_job_cards
        from db.database import get_session, init_db
        from db.models import Job
        from datetime import datetime, timezone

        init_db()

        with open(Path(__file__).parent / "config" / "profile.yaml", encoding="utf-8") as f:
            profile = yaml.safe_load(f)

        jobs = _run_async(scrape_hiremetech(max_jobs=100))
        session = get_session()
        new_count = 0
        passing = []

        for job_data in jobs:
            existing = session.query(Job).filter(Job.job_id == job_data["job_id"]).first()
            if existing:
                continue

            try:
                result = _run_async(score_job(job_data, profile))
            except Exception as e:
                logger.error(f"Score failed {job_data['job_id']}: {e}")
                continue

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
                status="scored",
            )
            session.add(db_job)
            new_count += 1

            if should_keep(result):
                enriched = {**job_data, **result}
                passing.append(enriched)
                db_job.status = "notified"

        session.commit()
        session.close()

        if passing:
            send_job_cards(passing)
            logger.success(f"Scan complete: {new_count} new, {len(passing)} sent")
        else:
            from core.notifier import send_whatsapp
            send_whatsapp(f"🔍 סריקה הושלמה — {new_count} משרות חדשות, אין התאמות חדשות.")

    thread = threading.Thread(target=run_scan, daemon=True)
    thread.start()
    return "🔍 סריקה התחילה! תקבל עדכון כשמסתיים..."


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming = (request.form.get("Body") or "").strip()
    sender = request.form.get("From") or ""
    logger.info(f"Incoming WhatsApp from {sender}: {incoming!r}")

    resp = MessagingResponse()
    upper = incoming.upper()

    if upper.startswith("APPLY "):
        job_id = incoming[6:].strip().upper()
        reply = _handle_apply(job_id)

    elif upper == "STATUS":
        reply = _handle_status()

    elif upper == "SCAN":
        reply = _handle_scan()

    elif upper in ("HELP", "עזרה", "?"):
        reply = (
            "🤖 *JobTracker — פקודות זמינות:*\n\n"
            "APPLY HMT-{id} — הגש למשרה\n"
            "STATUS — הצג סטטיסטיקות\n"
            "SCAN — סרוק משרות חדשות עכשיו"
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
    port = int(os.environ.get("WEBHOOK_PORT", 5000))
    logger.info(f"Starting webhook server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
