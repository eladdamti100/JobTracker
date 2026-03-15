"""WhatsApp notifications via Twilio — job suggestion cards with YES/NO/SKIP."""

import os
import time
from twilio.rest import Client
from loguru import logger


def _get_client() -> Client:
    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


def send_whatsapp(message: str) -> bool:
    """Send a single WhatsApp message. Returns True on success."""
    try:
        client = _get_client()
        msg = client.messages.create(
            from_=os.environ["TWILIO_WHATSAPP_FROM"],
            to=os.environ["MY_WHATSAPP_NUMBER"],
            body=message,
        )
        logger.info(f"WhatsApp sent. SID: {msg.sid}")
        return True
    except Exception as e:
        logger.error(f"WhatsApp send failed: {e}")
        return False


def format_suggestion_card(job: dict) -> str:
    """Format a job as a WhatsApp suggestion card with YES/NO/SKIP options."""
    level_map = {"student": "סטודנט", "junior": "ג׳וניור", "senior": "סניור"}
    level_display = level_map.get(job.get("level", ""), job.get("job_level", "לא ידוע"))

    reason = job.get("reason") or job.get("role_summary") or ""
    apply_url = job.get("apply_url", "")

    return (
        f'🆕 *New Job Match!*\n\n'
        f'🏢 *{job["company"]}* — {job["title"]}\n'
        f'👤 Level: {level_display}\n'
        f'🎯 Score: {job.get("score", "?")}/10\n'
        f'💬 {reason}\n'
        f'🔗 {apply_url}\n\n'
        f'Reply:\n'
        f'✅ *YES* — to apply automatically\n'
        f'❌ *NO* — to skip forever\n'
        f'⏰ *SKIP* — remind me in 12 hours'
    )


def send_suggestion(job: dict, delay_sec: float = 0.5) -> bool:
    """Send a single job suggestion card via WhatsApp. Returns True on success."""
    card = format_suggestion_card(job)
    return send_whatsapp(card)


def send_suggestions(jobs: list[dict], delay_sec: float = 1.0) -> tuple[int, int]:
    """Send multiple job suggestion cards via WhatsApp.

    Returns (sent_count, failed_count).
    """
    sent, failed = 0, 0
    logger.info(f"Sending {len(jobs)} job suggestions via WhatsApp...")

    if len(jobs) > 1:
        send_whatsapp(
            f"🤖 *JobTracker* — נמצאו {len(jobs)} משרות מתאימות!\n"
            f"כל משרה תגיע בהודעה נפרדת 👇"
        )
        time.sleep(delay_sec)

    for job in jobs:
        ok = send_suggestion(job)
        if ok:
            sent += 1
        else:
            failed += 1
        if delay_sec > 0:
            time.sleep(delay_sec)

    logger.info(f"Suggestions: {sent} sent, {failed} failed")
    return sent, failed


# Legacy aliases for backwards compatibility
def format_job_card(job: dict) -> str:
    return format_suggestion_card(job)

def send_job_cards(jobs: list[dict], delay_sec: float = 1.0) -> tuple[int, int]:
    return send_suggestions(jobs, delay_sec)

def send_whatsapp_jobs(jobs: list[dict]) -> bool:
    sent, failed = send_suggestions(jobs)
    return failed == 0

def format_job_message(jobs: list[dict]) -> str:
    """Legacy single-message format."""
    header = f"🤖 *JobTracker* — נמצאו {len(jobs)} משרות מתאימות:\n"
    lines = [header]
    for job in jobs:
        salary = job.get("salary") or "לא צוין"
        lines.append(
            f'✅ *{job["company"]}* — {job["title"]}\n'
            f'📍 {job.get("location", "N/A")} | 💰 {salary}\n'
            f'🎯 ציון: {job["score"]}/10 | 💬 {job.get("reason", "")}\n'
            f'🔗 {job["apply_url"]}\n'
        )
    return "\n\n".join(lines)
