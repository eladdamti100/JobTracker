"""Scraper for hiremetech.com via their public /api/jobs/search endpoint."""

import time
import urllib.request
import urllib.parse
import json
from datetime import date
from loguru import logger

BASE_URL = "https://hiremetech.com/api/jobs/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://hiremetech.com/jobs",
}

# Query combos to maximize student/intern coverage for Israeli jobs
SEARCH_QUERIES = [
    {"is_israeli": "true", "job_level": "internship", "sort_by": "posted_date", "sort_order": "desc"},
    {"is_israeli": "true", "job_level": "student",    "sort_by": "posted_date", "sort_order": "desc"},
    {"is_israeli": "true", "q": "סטודנט",             "sort_by": "posted_date", "sort_order": "desc"},
    {"is_israeli": "true", "q": "student intern junior", "sort_by": "posted_date", "sort_order": "desc"},
]

PAGE_SIZE = 50


def _make_job_id(raw_id: int) -> str:
    return f"HMT-{raw_id}"


def _extract_location(loc: dict) -> str:
    if not loc:
        return ""
    basic = loc.get("basic") or {}
    parts = [basic.get("city"), basic.get("country")]
    work_model = loc.get("work_model") or {}
    tag = work_model.get("display_tag")
    location_str = ", ".join(p for p in parts if p)
    if tag and tag != "On-site":
        location_str = f"{location_str} ({tag})" if location_str else tag
    return location_str


def _extract_salary(sal: dict) -> str | None:
    if not sal:
        return None
    mn, mx, cur = sal.get("min"), sal.get("max"), sal.get("currency", "")
    if mn and mx:
        return f"{mn:,}-{mx:,} {cur}"
    if mn:
        return f"מ-{mn:,} {cur}"
    return None


def _parse_posted_at(raw: dict) -> str:
    """Return posted date as a formatted string, e.g. '11/03/2026'."""
    d = raw.get("posted_date") or raw.get("date_posted")
    if not d:
        return "לא ידוע"
    try:
        # API returns ISO format: "2026-03-10"
        parsed = date.fromisoformat(str(d))
        return parsed.strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return str(d)


def _fetch_page(params: dict, offset: int) -> list[dict]:
    p = {**params, "limit": PAGE_SIZE, "offset": offset}
    url = BASE_URL + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace"))
    return data.get("jobs", [])


def _normalize(raw: dict) -> dict:
    return {
        "job_id": _make_job_id(raw["id"]),
        "title": raw.get("title", ""),
        "company": raw.get("company_name", ""),
        "location": _extract_location(raw.get("location")),
        "description": raw.get("description", "") or "",
        "apply_url": raw.get("job_url", ""),
        "date_posted": raw.get("posted_date", ""),
        "posted_at": _parse_posted_at(raw),   # formatted for display
        "salary": _extract_salary(raw.get("salary")),
        "job_level": raw.get("job_level", ""),
    }


async def scrape_hiremetech(max_jobs: int = 200) -> list[dict]:
    """Scrape job listings from hiremetech.com.

    Runs multiple targeted queries (internship, student, Hebrew terms)
    and deduplicates by job_id.

    Returns list of normalized job dicts.
    """
    logger.info("Starting hiremetech.com scrape...")
    seen_ids: set[str] = set()
    all_jobs: list[dict] = []

    for query_params in SEARCH_QUERIES:
        logger.info(f"Query: {query_params}")
        offset = 0

        while len(all_jobs) < max_jobs:
            try:
                raw_jobs = _fetch_page(query_params, offset)
            except Exception as e:
                logger.warning(f"Fetch failed (offset={offset}): {e}")
                break

            if not raw_jobs:
                break

            new_this_batch = 0
            for raw in raw_jobs:
                job_id = _make_job_id(raw["id"])
                if job_id not in seen_ids:
                    seen_ids.add(job_id)
                    all_jobs.append(_normalize(raw))
                    new_this_batch += 1

            logger.info(
                f"  offset={offset}: got {len(raw_jobs)}, "
                f"new={new_this_batch}, total unique={len(all_jobs)}"
            )

            if new_this_batch == 0 or len(raw_jobs) < PAGE_SIZE:
                break

            offset += PAGE_SIZE
            time.sleep(0.5)

        if len(all_jobs) >= max_jobs:
            break

    logger.success(f"Scrape complete: {len(all_jobs)} unique jobs collected")
    return all_jobs
