"""
WhatsApp Bridge — Flask server on port 5001.

Receives job URLs from whatsapp_group.js, scrapes them with Playwright,
scores with Claude, saves to DB, and notifies via WhatsApp if relevant.

Run: python scanners/whatsapp_bridge.py
"""

import sys
import os
import re
import asyncio
import hashlib
import threading
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

# Ensure project root is importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from loguru import logger

load_dotenv(ROOT / ".env")

# Configure logging to project log file
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger.add(LOG_DIR / "whatsapp_bridge.log", rotation="10 MB", retention="14 days")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _extract_from_url(url: str) -> tuple[str, str]:
    """Extract title and company directly from the URL structure before scraping.

    Returns (title, company) — both may be empty strings if not determinable.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path

    # ── Workday: proofpoint.wd5.myworkdayjobs.com/…/Job-Title_RXXXXX ─────
    if "myworkdayjobs.com" in host:
        company = host.split(".")[0].capitalize()
        # Last path segment: "Automation-Engineer-Intern_R13779" → title
        slug = path.rstrip("/").split("/")[-1]
        slug = re.sub(r"_[A-Z0-9]+$", "", slug)   # strip requisition ID
        title = slug.replace("-", " ").replace("%20", " ").title()
        return title, company

    # ── Breezy: sefaria.breezy.hr/p/HASH-job-title-here ─────────────────
    if "breezy.hr" in host:
        company = host.split(".")[0].capitalize()
        slug = path.rstrip("/").split("/")[-1]
        slug = re.sub(r"^[a-f0-9]{12,}-", "", slug)   # strip leading hash
        # Strip trailing location keywords
        slug = re.sub(r"-(israel|tel-aviv|ramat-gan|remote)$", "", slug, flags=re.IGNORECASE)
        title = slug.replace("-", " ").title()
        return title, company

    # ── Greenhouse: company.greenhouse.io/jobs/… ─────────────────────────
    if "greenhouse.io" in host:
        company = host.split(".")[0].capitalize()
        return "", company

    # ── Lever: jobs.lever.co/company/… ──────────────────────────────────
    if "lever.co" in host:
        parts = [p for p in path.split("/") if p]
        company = parts[0].replace("-", " ").title() if parts else ""
        return "", company

    # ── LinkedIn: linkedin.com/jobs/view/NUMERIC_ID ──────────────────────
    # Can't extract title from URL alone — rely on page scraping
    if "linkedin.com" in host:
        return "", ""

    return "", ""


async def scrape_job_page(url: str) -> dict:
    """Load a job URL with Playwright and extract title, company, and body text.

    Uses URL-based extraction as primary source for title/company, falls back
    to page content (h1, og:title, og:site_name).
    """
    from playwright.async_api import async_playwright

    # Pre-extract from URL structure (fast, no network needed)
    url_title, url_company = _extract_from_url(url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
                )
            )
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            body = await page.evaluate("() => document.body.innerText")

            # Extract title: h1 first, then og:title, then page title
            scraped_title = await page.evaluate("""() => {
                const h1 = document.querySelector('h1');
                const og = document.querySelector('meta[property="og:title"]');
                const title = document.title;
                return (h1 && h1.innerText.trim()) ||
                       (og && og.content.trim()) ||
                       title || '';
            }""")

            # Extract company: og:site_name, application-name, or nothing
            scraped_company = await page.evaluate("""() => {
                const og = document.querySelector('meta[property="og:site_name"]');
                const app = document.querySelector('meta[name="application-name"]');
                return (og && og.content.trim()) || (app && app.content.trim()) || '';
            }""")

            # Resolution order: URL-derived > page-scraped
            title = url_title or scraped_title or ""
            company = url_company or scraped_company or ""

            # Clean up generic page titles that are just the site name
            generic_titles = {"careers", "jobs", "career", "job board", "apply now", ""}
            if title.lower().strip() in generic_titles:
                title = url_title or scraped_title or ""

            logger.debug(f"Extracted — title={repr(title)} company={repr(company)} url={url}")

            return {
                "title": title.strip(),
                "company": company.strip(),
                "description": body[:4000].strip(),
            }
        except Exception as e:
            logger.error(f"Scrape failed for {url}: {e}")
            return {"title": url_title, "company": url_company, "description": ""}
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Processing pipeline
# ---------------------------------------------------------------------------

def _make_job_id(url: str) -> str:
    return f"WA-{hashlib.md5(url.encode()).hexdigest()[:10].upper()}"


def _url_exists(url: str) -> bool:
    from db.database import get_session, init_db
    from db.models import Job

    init_db()
    session = get_session()
    try:
        return session.query(Job).filter(Job.apply_url == url).first() is not None
    finally:
        session.close()


async def _process(url: str, group_name: str, hint_title: str = "", hint_company: str = ""):
    """Full pipeline: scrape → score → save → notify."""
    import yaml
    from core.analyzer import score_job, should_keep
    from core.notifier import send_job_cards
    from db.database import get_session, init_db
    from db.models import Job

    logger.info(f"Processing URL from '{group_name}': {url}")

    # Scrape
    scraped = await scrape_job_page(url)
    title = hint_title or scraped["title"] or "Unknown Position"
    # Never fall back to the WhatsApp group name as company
    company = hint_company or scraped["company"] or urlparse(url).hostname or "Unknown Company"
    description = scraped["description"]

    if not description:
        logger.warning(f"Empty description for {url}, skipping")
        return

    job_data = {
        "job_id": _make_job_id(url),
        "title": title,
        "company": company,
        "location": "ישראל",
        "description": description,
        "apply_url": url,
        "date_posted": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "posted_at": datetime.now(timezone.utc).strftime("%d/%m/%Y"),
        "salary": None,
        "source": f"WhatsApp: {group_name}",
    }

    # Score
    with open(ROOT / "config" / "profile.yaml", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    try:
        result = await score_job(job_data, profile)
    except Exception as e:
        logger.error(f"Claude scoring failed for {url}: {e}")
        return

    enriched = {**job_data, **result}

    # Save to DB
    init_db()
    session = get_session()
    try:
        # Check again inside transaction (race condition guard)
        if session.query(Job).filter(Job.apply_url == url).first():
            logger.info(f"Duplicate (race), skipping: {url}")
            return

        db_job = Job(
            job_id=job_data["job_id"],
            title=title,
            company=company,
            location=job_data["location"],
            description=description,
            apply_url=url,
            date_posted=job_data["date_posted"],
            salary=None,
            score=result["score"],
            level=result.get("level"),
            role_type=result.get("role_type"),
            tech_stack_match=result.get("tech_stack_match"),
            is_student_position=int(result.get("is_student_position", False)),
            apply_strategy=result.get("apply_strategy"),
            role_summary=result.get("role_summary"),
            requirements_summary=result.get("requirements_summary"),
            status="scored",
        )
        session.add(db_job)

        if should_keep(result):
            db_job.status = "notified"
            session.commit()
            send_job_cards([enriched])
            logger.success(
                f"Sent to WhatsApp: '{title}' @ {company} "
                f"[{result.get('level')}] score={result['score']}/10"
            )
        else:
            session.commit()
            logger.info(
                f"Skipped (score={result['score']}, level={result.get('level')}): {title}"
            )
    except Exception as e:
        logger.error(f"DB save failed: {e}")
        session.rollback()
    finally:
        session.close()


def _run_in_thread(url: str, group_name: str, hint_title: str, hint_company: str):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_process(url, group_name, hint_title, hint_company))
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Flask endpoints
# ---------------------------------------------------------------------------

@app.route("/new_job", methods=["POST"])
def new_job():
    """Receive a job URL from the Node.js listener."""
    data = request.json or {}
    url = (data.get("url") or "").strip()
    group_name = data.get("group_name", "WhatsApp Group")
    hint_title = data.get("title", "")
    hint_company = data.get("company", "")

    if not url:
        return jsonify({"error": "url is required"}), 400

    if _url_exists(url):
        logger.info(f"Already in DB, skipping: {url}")
        return jsonify({"status": "duplicate"}), 200

    # Process in background so we return fast to Node.js
    thread = threading.Thread(
        target=_run_in_thread,
        args=(url, group_name, hint_title, hint_company),
        daemon=True,
    )
    thread.start()

    logger.info(f"Queued for processing: {url}")
    return jsonify({"status": "processing", "url": url}), 202


@app.route("/check_url", methods=["GET"])
def check_url():
    """Check if a URL is already stored in the DB."""
    url = request.args.get("url", "")
    if not url:
        return jsonify({"exists": False}), 200
    return jsonify({"exists": _url_exists(url)}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "whatsapp_bridge"}), 200


if __name__ == "__main__":
    from db.database import init_db
    init_db()
    port = int(os.environ.get("BRIDGE_PORT", 5001))
    logger.info(f"WhatsApp bridge starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
