"""APScheduler setup for periodic job scanning."""

from loguru import logger


def start_scheduler():
    """Start the APScheduler to run scans every 12 hours."""
    # TODO: Implement scheduler
    logger.info("Starting scheduler (every 12 hours)...")
    raise NotImplementedError("Scheduler not yet implemented")
