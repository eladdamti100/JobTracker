"""Expire old suggested jobs that got no WhatsApp response."""

from datetime import datetime, timezone
from loguru import logger

from db.database import get_session
from db.models import SuggestedJob


def expire_old_suggestions() -> int:
    """Find all suggested_jobs where status='suggested' AND expires_at < now.
    Update status to 'expired'. Returns count of expired jobs.
    """
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        expired_jobs = (
            session.query(SuggestedJob)
            .filter(
                SuggestedJob.status == "suggested",
                SuggestedJob.expires_at < now,
            )
            .all()
        )

        count = 0
        for job in expired_jobs:
            job.status = "expired"
            logger.info(f"Job expired without response: {job.company} — {job.title}")
            count += 1

        if count > 0:
            session.commit()

        return count
    finally:
        session.close()
