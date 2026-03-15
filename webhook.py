"""
Inbound WhatsApp webhook via Flask + Twilio.

Human-in-the-loop flow:
  YES    → approve last suggested job, ask for feedback/instructions
           (2nd reply) "כן"/"go"/"submit" → apply immediately
           (2nd reply) free text          → apply with instruction injected into cover letter
           (2nd reply) "המתן"/"wait"      → hold, don't apply yet
  NO     → reject last suggested job permanently
  SKIP   → snooze for 12 hours
  STATUS → show stats summary
  SCAN   → trigger immediate scan
  WAIT   → pause a pending application (while in awaiting_feedback state)

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


def _get_conversation_state():
    """Return the single ConversationState row."""
    from db.database import get_session
    from db.models import ConversationState
    session = get_session()
    try:
        state = session.query(ConversationState).first()
        if state:
            from sqlalchemy.orm import make_transient
            session.expunge(state)
        return state
    finally:
        session.close()


def _set_conversation_state(state: str, job_hash: str = None,
                             field_label: str = None, field_answer: str = None):
    """Update the single ConversationState row."""
    from db.database import get_session
    from db.models import ConversationState
    session = get_session()
    try:
        row = session.query(ConversationState).first()
        if not row:
            row = ConversationState()
            session.add(row)
        row.state = state
        row.pending_job_hash = job_hash
        row.pending_field_label = field_label
        row.field_answer = field_answer
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()


def _spawn_apply_thread(job_hash: str, company: str, title: str,
                        apply_url: str, description: str, user_instruction: str = ""):
    """Spawn a background thread to run auto-apply and notify via WhatsApp."""
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
            user_instruction=user_instruction,
        )

        s = gs()
        try:
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
                application_method=result.get("application_result", "auto_apply")
                    if result.get("application_result") == "easy_apply" else "auto_apply",
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
        # Reset conversation state after apply completes
        _set_conversation_state("idle")

    thread = threading.Thread(target=run_apply, daemon=True)
    thread.start()


def _handle_yes() -> str:
    """Stage 1: Approve the last suggested job, then ask for instructions."""
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
            return "❌ אין משרות ממתינות לאישור."

        job.status = "approved"
        job.responded_at = datetime.now(timezone.utc)
        session.commit()

        _set_conversation_state("awaiting_feedback", job_hash=job.job_hash)

        return (
            f"👍 *{job.company} — {job.title}*\n\n"
            f"🎯 כל הערות לפני שאגיש?\n"
            f"• כתוב הנחיה (למשל: _'הדגש Docker'_, _'השתמש ב-CV של DevOps'_)\n"
            f"• *כן* — הגש ישירות\n"
            f"• *המתן* — עצור, אגיש מאוחר יותר"
        )
    finally:
        session.close()


def _handle_feedback(text: str) -> str:
    """Stage 2: Process the user's reply after YES — apply with optional instruction."""
    from db.database import get_session
    from db.models import SuggestedJob

    conv = _get_conversation_state()
    if not conv or not conv.pending_job_hash:
        _set_conversation_state("idle")
        return "❌ לא מצאתי משרה ממתינה. שלח YES כדי להתחיל."

    job_hash = conv.pending_job_hash
    upper = text.upper().strip()

    # User wants to hold — don't apply yet
    if upper in ("המתן", "WAIT", "HOLD", "עצור", "STOP"):
        _set_conversation_state("idle")
        return "⏸ בסדר, לא מגיש עכשיו. שלח *YES* שוב כשתהיה מוכן."

    # Fetch job details
    session = get_session()
    try:
        job = session.query(SuggestedJob).filter_by(job_hash=job_hash).first()
        if not job:
            _set_conversation_state("idle")
            return "❌ לא מצאתי את המשרה בבסיס הנתונים."

        company = job.company
        title = job.title
        apply_url = job.apply_url
        description = job.description or ""
    finally:
        session.close()

    # Determine instruction (empty = "כן"/"yes"/"go" = apply as-is)
    confirm_words = {"כן", "YES", "Y", "GO", "SUBMIT", "OK", "אוקי", "בסדר"}
    instruction = "" if upper in confirm_words else text.strip()

    # Confirm to user what we're about to do
    if instruction:
        confirm_msg = f"⚙️ מגיש ל-*{company}* עם הנחיה:\n_{instruction}_\n\nאעדכן כשזה יסתיים."
    else:
        confirm_msg = f"⚙️ מגיש ל-*{company}* — {title}...\nאעדכן כשזה יסתיים."

    _set_conversation_state("idle")
    _spawn_apply_thread(job_hash, company, title, apply_url, description, instruction)

    return confirm_msg


def _handle_no() -> str:
    """Reject the last suggested job permanently."""
    from db.database import get_session
    from db.models import SuggestedJob

    _set_conversation_state("idle")

    session = get_session()
    try:
        job = (
            session.query(SuggestedJob)
            .filter(SuggestedJob.status.in_(["suggested", "approved"]))
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

    _set_conversation_state("idle")

    session = get_session()
    try:
        job = (
            session.query(SuggestedJob)
            .filter(SuggestedJob.status.in_(["suggested", "approved"]))
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

    # --- Check conversation state first ---
    # If we're waiting for the user's feedback/instruction after YES,
    # route everything (except global commands) into the feedback handler.
    conv = _get_conversation_state()
    in_feedback = conv and conv.state == "awaiting_feedback"

    # Global commands always work regardless of state
    if upper in ("NO", "לא", "N"):
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
            "❌ *NO* — בטל / דחה\n"
            "⏰ *SKIP* — הזכר לי בעוד 12 שעות\n"
            "📊 *STATUS* — הצג סטטיסטיקות\n"
            "🔍 *SCAN* — סרוק משרות חדשות עכשיו\n\n"
            "_לאחר YES: כתוב הנחיה, 'כן' להגשה ישירה, או 'המתן' לעצירה._"
        )

    elif in_feedback:
        # Any other message after YES → treat as feedback/instruction
        reply = _handle_feedback(incoming)

    elif upper in ("YES", "כן", "Y"):
        reply = _handle_yes()

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
