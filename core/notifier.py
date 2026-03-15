"""WhatsApp notifications via Twilio — job suggestion cards with YES/NO/SKIP."""

import json
import os
import time
from twilio.rest import Client
from loguru import logger

# Cache of content template SIDs keyed by job_hash
_content_template_cache = {}


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
    """Format a job as a WhatsApp suggestion card body (used inside quick-reply template)."""
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
        f'🔗 {apply_url}'
    )


def _get_or_create_template(job_hash: str) -> str:
    """Get or create a Twilio Content Template with job_hash in button action IDs."""
    short_hash = job_hash[:8]
    if short_hash in _content_template_cache:
        return _content_template_cache[short_hash]

    import requests
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]

    payload = {
        "friendly_name": f"job_{short_hash}",
        "language": "en",
        "types": {
            "twilio/quick-reply": {
                "body": "{{1}}",
                "actions": [
                    {"title": "YES", "id": f"yes_{short_hash}"},
                    {"title": "NO", "id": f"no_{short_hash}"},
                    {"title": "SKIP", "id": f"skip_{short_hash}"},
                ]
            }
        },
        "variables": {"1": "job details"},
    }

    resp = requests.post(
        "https://content.twilio.com/v1/Content",
        json=payload,
        auth=(account_sid, auth_token),
    )
    data = resp.json()
    content_sid = data["sid"]
    _content_template_cache[short_hash] = content_sid
    logger.info(f"Created content template {content_sid} for job {short_hash}")
    return content_sid


def send_suggestion(job: dict, delay_sec: float = 0.5) -> bool:
    """Send a job suggestion card with quick-reply buttons (YES/NO/SKIP)."""
    card = format_suggestion_card(job)
    job_hash = job.get("job_hash", "")
    try:
        content_sid = _get_or_create_template(job_hash)
        client = _get_client()
        msg = client.messages.create(
            from_=os.environ["TWILIO_WHATSAPP_FROM"],
            to=os.environ["MY_WHATSAPP_NUMBER"],
            content_sid=content_sid,
            content_variables=json.dumps({"1": card}),
        )
        logger.info(f"WhatsApp suggestion sent with buttons. SID: {msg.sid}")
        return True
    except Exception as e:
        logger.error(f"WhatsApp suggestion send failed: {e}")
        return False


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


def send_application_result(company: str, title: str, success: bool,
                            error_message: str = "", screenshot_path: str = "") -> bool:
    """Notify the user about the result of an auto-application via WhatsApp."""
    if success:
        msg = (
            f"✅ *הגשה הצליחה!*\n\n"
            f"🏢 *{company}* — {title}\n"
            f"ההגשה בוצעה בהצלחה."
        )
    else:
        msg = (
            f"❌ *הגשה נכשלה*\n\n"
            f"🏢 *{company}* — {title}\n"
            f"שגיאה: {error_message[:300]}"
        )
        if screenshot_path:
            msg += f"\n📸 צילום מסך: {screenshot_path}"
    return send_whatsapp(msg)


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
