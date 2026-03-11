"""LinkedIn Jobs scanner — scrapes software intern positions in Israel (last 24h).

Uses Playwright to:
  1. Scrape the search results page for job URLs, titles, companies, locations
  2. Scrape each individual job page for the full description (one browser context reused)

Job IDs: LI-{numeric_linkedin_id}
"""

import re
import asyncio
from datetime import date
from loguru import logger
from playwright.async_api import async_playwright, BrowserContext

# Search queries — multiple keyword variants to maximize coverage
SEARCH_QUERIES = [
    "software intern",
    "software student",
    "backend intern",
    "fullstack intern",
    "devops intern",
]

SEARCH_BASE = (
    "https://www.linkedin.com/jobs/search/"
    "?location=Israel"
    "&f_TPR=r86400"   # last 24 hours
    "&f_E=1"          # experience level: Internship
    "&keywords={keywords}"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _job_id(url: str) -> str:
    m = re.search(r"/jobs/view/(\d+)", url)
    return f"LI-{m.group(1)}" if m else None


def _clean_url(url: str) -> str:
    """Strip tracking params — keep only the base /jobs/view/{id} path."""
    m = re.search(r"(https://www\.linkedin\.com/jobs/view/\d+)", url)
    return m.group(1) if m else url.split("?")[0]


async def _scroll_results(page) -> None:
    """Scroll the LinkedIn results pane to trigger lazy-loading."""
    # Try the specific results container first, fall back to window scroll
    for _ in range(6):
        await page.evaluate("""() => {
            const pane = document.querySelector(
                '.jobs-search__results-list, ' +
                '.scaffold-layout__list-container, ' +
                'ul[class*="jobs-search"]'
            );
            if (pane) pane.scrollTop += 1200;
            else window.scrollBy(0, 1200);
        }""")
        await page.wait_for_timeout(1200)


async def _extract_cards(page) -> list[dict]:
    """Extract job cards from the LinkedIn search results page."""
    return await page.evaluate("""() => {
        const results = [];
        // Try multiple card selector patterns (LinkedIn changes these frequently)
        const cardSels = [
            'li.jobs-search__results-list-item',
            '.base-card',
            'div[data-entity-urn*="jobPosting"]',
            '.job-search-card',
        ];
        let cards = [];
        for (const sel of cardSels) {
            cards = Array.from(document.querySelectorAll(sel));
            if (cards.length > 0) break;
        }

        cards.forEach(card => {
            // Job link
            const linkEl = card.querySelector('a[href*="/jobs/view/"]');
            if (!linkEl) return;
            const href = linkEl.href.split('?')[0];
            if (!href.includes('/jobs/view/')) return;

            // Title
            const titleEl = card.querySelector(
                'h3.base-search-card__title, ' +
                '.job-card-list__title, ' +
                'h3[class*="title"], ' +
                '.base-card__title, ' +
                'span[aria-hidden="true"]'
            );
            const title = (titleEl && titleEl.innerText.trim()) || linkEl.innerText.trim();

            // Company
            const compEl = card.querySelector(
                'h4.base-search-card__subtitle, ' +
                '.job-card-container__company-name, ' +
                'h4[class*="company"], ' +
                '.base-card__subtitle, ' +
                'a[data-tracking-control-name*="company"]'
            );
            const company = compEl ? compEl.innerText.trim() : '';

            // Location
            const locEl = card.querySelector(
                '.job-search-card__location, ' +
                '.job-card-container__metadata-item, ' +
                'span[class*="location"]'
            );
            const location = locEl ? locEl.innerText.trim() : 'Israel';

            // Short snippet (may be empty)
            const snipEl = card.querySelector(
                '.job-search-card__snippet, p[class*="description"]'
            );
            const snippet = snipEl ? snipEl.innerText.trim() : '';

            results.push({ href, title, company, location, snippet });
        });

        return results;
    }""")


async def _scrape_job_description(context: BrowserContext, url: str) -> str:
    """Open a job page in a new tab and extract the full description text."""
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2000)

        description = await page.evaluate("""() => {
            // Try dedicated description container first
            const containers = [
                '.show-more-less-html__markup',
                '.description__text',
                '.jobs-description__content',
                '#job-details',
                'section[class*="description"]',
            ];
            for (const sel of containers) {
                const el = document.querySelector(sel);
                if (el && el.innerText.trim().length > 100) {
                    return el.innerText.trim();
                }
            }
            // Fall back to full body text
            return document.body.innerText.trim();
        }""")
        return description[:4000]
    except Exception as e:
        logger.warning(f"Description scrape failed for {url}: {e}")
        return ""
    finally:
        await page.close()


async def scrape_linkedin(max_jobs: int = 40) -> list[dict]:
    """Scrape LinkedIn job search for software intern positions in Israel (last 24h).

    Returns list of normalized job dicts matching the hiremetech format.
    """
    logger.info("Starting LinkedIn scrape (software intern, Israel, last 24h)...")

    seen_ids: set[str] = set()
    card_map: dict[str, dict] = {}   # job_id → card data

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        # ── Step 1: scrape search result pages ─────────────────────────────
        for keywords in SEARCH_QUERIES:
            if len(seen_ids) >= max_jobs:
                break

            url = SEARCH_BASE.format(keywords=keywords.replace(" ", "%20"))
            logger.info(f"  Query: {keywords}")

            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)
                await _scroll_results(page)

                cards = await _extract_cards(page)
                logger.info(f"  Found {len(cards)} cards for '{keywords}'")

                for card in cards:
                    job_id = _job_id(card["href"])
                    if not job_id or job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)
                    card_map[job_id] = card

                    if len(seen_ids) >= max_jobs:
                        break

            except Exception as e:
                logger.warning(f"  Search page failed for '{keywords}': {e}")
            finally:
                await page.close()

            await asyncio.sleep(1)

        logger.info(f"Collected {len(card_map)} unique job cards — scraping descriptions...")

        # ── Step 2: scrape each individual job page for description ─────────
        today = date.today().strftime("%Y-%m-%d")
        today_display = date.today().strftime("%d/%m/%Y")

        all_jobs: list[dict] = []
        for job_id, card in card_map.items():
            clean_url = _clean_url(card["href"])
            description = card.get("snippet") or ""

            # Only fetch full page if snippet is too short for good scoring
            if len(description) < 80:
                description = await _scrape_job_description(context, clean_url)

            all_jobs.append({
                "job_id": job_id,
                "title": card.get("title") or "Software Intern",
                "company": card.get("company") or "",
                "location": card.get("location") or "Israel",
                "description": description,
                "apply_url": clean_url,
                "date_posted": today,
                "posted_at": today_display,
                "salary": None,
                "source": "LinkedIn",
            })
            await asyncio.sleep(0.5)

        await browser.close()

    logger.success(f"LinkedIn scrape complete: {len(all_jobs)} jobs")
    return all_jobs
