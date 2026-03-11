"""WhatsApp notifications via Twilio — one message per job card."""

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


def format_job_card(job: dict) -> str:
    """Format a single job as a clean WhatsApp card message."""
    level_map = {"student": "סטודנט", "junior": "ג׳וניור", "senior": "סניור"}
    level_display = level_map.get(job.get("level", ""), job.get("job_level", "לא ידוע"))

    role_summary = job.get("role_summary") or "לא צוין"
    req_summary = job.get("requirements_summary") or "לא צוין"
    posted = job.get("posted_at") or job.get("date_posted") or "לא ידוע"
    apply_url = job.get("apply_url", "")
    job_id = job.get("job_id", "")

    return (
        f'🏢 *{job["company"]}* — {job["title"]}\n\n'
        f'📋 *תפקיד:* {role_summary}\n\n'
        f'🎯 *דרישות:* {req_summary}\n\n'
        f'👤 *רמה:* {level_display}\n'
        f'📅 *פורסם:* {posted}\n'
        f'🔗 {apply_url}\n\n'
        f'APPLY {job_id}'
    )


def send_job_cards(jobs: list[dict], delay_sec: float = 1.0) -> tuple[int, int]:
    """Send each job as a separate WhatsApp message card.

    Returns (sent_count, failed_count).
    """
    sent, failed = 0, 0
    logger.info(f"Sending {len(jobs)} job cards via WhatsApp...")

    # Header message
    send_whatsapp(
        f"🤖 *JobTracker* — נמצאו {len(jobs)} משרות מתאימות!\n"
        f"כל משרה תגיע בהודעה נפרדת 👇"
    )
    time.sleep(delay_sec)

    for job in jobs:
        card = format_job_card(job)
        ok = send_whatsapp(card)
        if ok:
            sent += 1
        else:
            failed += 1
        if delay_sec > 0:
            time.sleep(delay_sec)

    logger.info(f"Job cards: {sent} sent, {failed} failed")
    return sent, failed


# Keep for backwards compatibility with test_phase1.py
def send_whatsapp_jobs(jobs: list[dict]) -> bool:
    sent, failed = send_job_cards(jobs)
    return failed == 0


def format_job_message(jobs: list[dict]) -> str:
    """Legacy single-message format — kept for test_phase1.py."""
    header = f"🤖 *JobTracker* — נמצאו {len(jobs)} משרות מתאימות:\n"
    lines = [header]
    for job in jobs:
        salary = job.get("salary") or "לא צוין"
        lines.append(
            f'✅ *{job["company"]}* — {job["title"]}\n'
            f'📍 {job.get("location", "N/A")} | 💰 {salary}\n'
            f'🎯 ציון: {job["score"]}/10 | 💬 {job.get("reason", "")}\n'
            f'🔗 {job["apply_url"]}\n'
            f'APPLY {job["job_id"]}'
        )
    return "\n\n".join(lines)
