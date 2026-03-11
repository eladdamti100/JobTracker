"""WhatsApp notifications via Twilio."""

from loguru import logger


def send_whatsapp(message: str) -> bool:
    """Send a WhatsApp message via Twilio sandbox.

    Returns True on success, False on failure.
    """
    # TODO: Implement Twilio WhatsApp sending
    logger.info("Sending WhatsApp notification...")
    raise NotImplementedError("Notifier not yet implemented")


def format_job_message(jobs: list[dict]) -> str:
    """Format scored jobs into a WhatsApp-friendly message."""
    lines = []
    for job in jobs:
        salary = job.get("salary") or "לא צוין"
        lines.append(
            f'✅ *{job["company"]}* — {job["title"]}\n'
            f'📍 {job.get("location", "N/A")} | 💰 {salary}\n'
            f'🎯 ציון התאמה: {job["score"]}/10\n'
            f'💬 {job["reason"]}\n'
            f'🔗 {job["apply_url"]}\n'
            f'להגשה אוטומטית שלח: APPLY {job["job_id"]}'
        )
    return "\n\n".join(lines)
