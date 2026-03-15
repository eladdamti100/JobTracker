"""LinkedIn Jobs scanner — scrapes software intern positions in Israel (last 24h).

Uses Playwright to:
  1. Login to LinkedIn (reuses saved session from data/linkedin_session.json)
  2. Scrape the search results page for job URLs, titles, companies, locations
  3. Scrape each individual job page for the full description (one browser context reused)

Job IDs: LI-{numeric_linkedin_id}

Credentials: set LINKEDIN_EMAIL and LINKEDIN_PASSWORD in .env
"""

import re
import json
import os
import asyncio
from datetime import date
from pathlib import Path
from loguru import logger
from playwright.async_api import async_playwright, BrowserContext

ROOT = Path(__file__).parent.parent
SESSION_FILE = ROOT / "data" / "linkedin_session.json"

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


def _is_logged_in_url(url: str) -> bool:
    """Return True if the URL is NOT a login/auth wall."""
    return (
        "linkedin.com/login" not in url
        and "linkedin.com/checkpoint" not in url
        and "linkedin.com/authwall" not in url
        and "linkedin.com/uas/" not in url
    )


async def _save_session(context: BrowserContext) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    cookies = await context.cookies()
    SESSION_FILE.write_text(json.dumps(cookies))
    logger.info("LinkedIn session saved")


async def _load_session(context: BrowserContext) -> bool:
    """Load saved session cookies. Returns True if file existed."""
    if not SESSION_FILE.exists():
        return False
    try:
        cookies = json.loads(SESSION_FILE.read_text())
        await context.add_cookies(cookies)
        logger.info("LinkedIn session loaded from file")
        return True
    except Exception as e:
        logger.warning(f"Failed to load LinkedIn session: {e}")
        return False


async def _login(context: BrowserContext, email: str, password: str) -> bool:
    """Login to LinkedIn. Returns True on success."""
    page = await context.new_page()
    try:
        logger.info("Logging in to LinkedIn...")
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        await page.fill("#username", email)
        await page.fill("#password", password)
        await page.click("button[type=submit]")
        # Wait up to 15s for redirect away from login page
        for _ in range(30):
            await page.wait_for_timeout(500)
            url = page.url
            if "login" not in url and "checkpoint" not in url and "uas" not in url:
                break
        url = page.url
        if not _is_logged_in_url(url):
            logger.error(f"Login failed or security challenge at: {url}")
            return False
        logger.success(f"LinkedIn login successful — at: {url}")
        await _save_session(context)
        return True
    except Exception as e:
        logger.error(f"LinkedIn login error: {e}")
        return False
    finally:
        await page.close()


async def _ensure_logged_in(context: BrowserContext) -> bool:
    """Ensure we have a valid LinkedIn session. Returns True if logged in."""
    email = os.environ.get("LINKEDIN_EMAIL", "")
    password = os.environ.get("LINKEDIN_PASSWORD", "")
    if not email or not password:
        logger.warning("LINKEDIN_EMAIL / LINKEDIN_PASSWORD not set — scraping without login")
        return False
    # Try loading existing session and verify it's still valid
    session_loaded = await _load_session(context)
    if session_loaded:
        page = await context.new_page()
        try:
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            url = page.url
            if _is_logged_in_url(url):
                logger.info("Existing LinkedIn session is valid")
                return True
            logger.info(f"Session expired (at {url}), re-logging in...")
        except Exception as e:
            logger.warning(f"Session check failed: {e}")
        finally:
            await page.close()
    # Login fresh
    return await _login(context, email, password)


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
        // Authenticated (logged-in) selectors first, then public/guest selectors
        const cardSels = [
            'li.scaffold-layout__list-item',
            '.job-card-container',
            '.jobs-search-results__list-item',
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
            // Job link — must contain /jobs/view/ with numeric ID
            const linkEl = card.querySelector('a[href*="/jobs/view/"]')
                || card.querySelector('a[href*="linkedin.com/jobs/view/"]');
            if (!linkEl) return;
            const rawHref = linkEl.href || linkEl.getAttribute('href') || '';
            const href = rawHref.split('?')[0];
            if (!/\\/jobs\\/view\\/\\d+/.test(href)) return;

            // Title — use aria-hidden span to avoid duplicate text
            const titleEl = card.querySelector(
                'a[class*="job-card-list__title"] span[aria-hidden="true"], ' +
                '.job-card-list__title--link span[aria-hidden="true"], ' +
                '.job-card-list__title, ' +
                'h3.base-search-card__title, ' +
                'h3[class*="title"], ' +
                '.base-card__title'
            );
            const title = (titleEl && titleEl.innerText.trim()) || linkEl.innerText.trim();

            // Company — authenticated: .artdeco-entity-lockup__subtitle; public: h4 subtitle
            const compEl = card.querySelector(
                '.artdeco-entity-lockup__subtitle, ' +
                'h4.base-search-card__subtitle, ' +
                '.job-card-container__company-name, ' +
                'h4[class*="company"], ' +
                '.base-card__subtitle'
            );
            const company = compEl ? compEl.innerText.trim() : '';

            // Location — authenticated: .artdeco-entity-lockup__caption; public: .job-search-card__location
            const locEl = card.querySelector(
                '.artdeco-entity-lockup__caption, ' +
                '.job-card-container__metadata-item, ' +
                '.job-search-card__location, ' +
                'span[class*="location"]'
            );
            const location = locEl ? locEl.innerText.trim() : 'Israel';

            // Short snippet (may be empty)
            const snipEl = card.querySelector(
                '.job-search-card__snippet, p[class*="description"]'
            );
            const snippet = snipEl ? snipEl.innerText.trim() : '';

            // Easy Apply detection — LinkedIn marks these with a badge/method label
            const easyApplyEl = card.querySelector(
                '.job-card-container__apply-method, ' +
                'li[class*="apply-method"], ' +
                'span[class*="easy-apply"]'
            );
            const easyApplyText = card.innerText || '';
            const isEasyApply = (easyApplyEl && easyApplyEl.innerText.includes('Easy Apply'))
                || easyApplyText.includes('Easy Apply');

            results.push({ href, title, company, location, snippet, isEasyApply });
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
            const containers = [
                '.jobs-description__content',
                '.jobs-description-content__text',
                '.show-more-less-html__markup',
                '.description__text',
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

        # ── Login ───────────────────────────────────────────────────────────
        logged_in = await _ensure_logged_in(context)
        if not logged_in:
            logger.warning("Proceeding without LinkedIn login — results may be limited")

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

                current_url = page.url
                logger.debug(f"  Landed at: {current_url}")
                if not _is_logged_in_url(current_url):
                    logger.warning(f"  Redirected to auth page for '{keywords}' — skipping")
                    continue

                await _scroll_results(page)

                cards = await _extract_cards(page)
                logger.info(f"  Found {len(cards)} cards for '{keywords}'")

                if cards:
                    c = cards[0]
                    logger.debug(f"  First card href={c.get('href','')[:80]}  title={c.get('title','')[:50]}")

                for card in cards:
                    job_id = _job_id(card["href"])
                    if not job_id:
                        logger.debug(f"  Skipped (no numeric job ID): {card.get('href','')[:60]}")
                        continue
                    if job_id in seen_ids:
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
                "is_easy_apply": bool(card.get("isEasyApply", False)),
            })
            await asyncio.sleep(0.5)

        await browser.close()

    logger.success(f"LinkedIn scrape complete: {len(all_jobs)} jobs")
    return all_jobs
