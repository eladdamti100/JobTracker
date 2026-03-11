"""Scraper for hiremetech.com — Israeli tech jobs site."""

from loguru import logger


async def scrape_hiremetech() -> list[dict]:
    """Scrape job listings from hiremetech.com.

    Returns list of dicts with keys:
        job_id, title, company, location, description,
        apply_url, date_posted, salary
    """
    # TODO: Implement Playwright scraping
    logger.info("Scraping hiremetech.com...")
    raise NotImplementedError("Scraper not yet implemented")
