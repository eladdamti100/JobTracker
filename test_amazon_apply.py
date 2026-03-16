#!/usr/bin/env python3
"""End-to-end test for the Amazon apply pipeline.

Simulates the flow:
  scan suggests Amazon job → you reply YES → auto-apply runs

Usage:
    python test_amazon_apply.py <amazon-job-url>
    python test_amazon_apply.py  # interactive prompt

The job is inserted into the DB as "approved" and the orchestrator runs
exactly as it would from webhook.py after a WhatsApp YES reply.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── project root on sys.path ──────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv()

from core.log_utils import setup_secure_logging
setup_secure_logging(log_dir=Path(__file__).parent / "logs")

from loguru import logger

# ─────────────────────────────────────────────────────────────────────────────

def _get_url() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1].strip()
    print("\nPaste an Amazon job URL (amazon.jobs or hiring.amazon.com):")
    print("Example: https://www.amazon.jobs/en/jobs/2345678/software-development-engineer")
    url = input("> ").strip()
    if not url:
        print("No URL provided — exiting.")
        sys.exit(1)
    return url


def _confirm_adapter(url: str) -> None:
    """Fail early if AmazonAdapter won't detect this URL."""
    from core.adapters.amazon_adapter import AmazonAdapter
    if not AmazonAdapter.detect(url):
        print(f"\n⚠  AmazonAdapter.detect() returned False for:\n   {url}")
        print("   Proceeding anyway — GenericAdapter may handle it.")


def _insert_or_get_job(url: str):
    """Insert the job as 'approved' if not already in DB, else reuse existing."""
    from db.database import init_db, get_session
    from db.models import SuggestedJob, make_job_hash

    init_db()

    # Extract a reasonable company/title from URL
    company = "Amazon"
    # Try to get job title from URL slug
    parts = [p for p in url.rstrip("/").split("/") if p and not p.isdigit()]
    raw_title = parts[-1].replace("-", " ").title() if parts else "Software Engineer"
    title = raw_title[:80]

    job_hash = make_job_hash(company, title, url)

    session = get_session()
    try:
        existing = session.query(SuggestedJob).filter_by(job_hash=job_hash).first()
        if existing:
            print(f"\nFound existing DB entry  [{existing.job_hash[:8]}]")
            print(f"  Status : {existing.status}")
            print(f"  Company: {existing.company}")
            print(f"  Title  : {existing.title}")
            if existing.status not in ("approved", "suggested"):
                print(f"\n  Status is '{existing.status}' — resetting to 'approved' for this test.")
                existing.status = "approved"
                session.commit()
            elif existing.status == "suggested":
                print("  Upgrading status: suggested → approved")
                existing.status = "approved"
                session.commit()
            return existing.job_hash, existing.company, existing.title

        # New job — create with minimal required fields
        print(f"\nInserting new test job  [{job_hash[:8]}]")
        job = SuggestedJob(
            job_hash=job_hash,
            company=company,
            title=title,
            source="Test",
            apply_url=url,
            location="Israel / Remote",
            description=f"Test job entry created by test_amazon_apply.py for URL: {url}",
            score=8.0,
            level="junior",
            role_type="engineering",
            status="approved",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=48),
        )
        session.add(job)
        session.commit()
        print(f"  Inserted: {company} — {title}")
        return job_hash, company, title

    finally:
        session.close()


def _run(job_hash: str, company: str, title: str, url: str) -> None:
    from db.database import get_session
    from db.models import SuggestedJob
    from core.orchestrator import run_application

    session = get_session()
    try:
        job = session.query(SuggestedJob).filter_by(job_hash=job_hash).first()
        if not job:
            print(f"ERROR: job {job_hash[:8]} not found in DB after insert.")
            sys.exit(1)

        print(f"\n{'='*60}")
        print(f"  APPLY TEST")
        print(f"{'='*60}")
        print(f"  Company : {company}")
        print(f"  Title   : {title}")
        print(f"  URL     : {url}")
        print(f"  Hash    : {job_hash[:8]}")
        print(f"{'='*60}\n")
        print("  auto_submit=False — will pause BEFORE clicking Submit.\n"
              "  Change to True (or pass --submit flag) to actually submit.\n")

        auto_submit = "--submit" in sys.argv

        result = run_application(job, auto_submit=auto_submit)

    finally:
        session.close()

    # ── Result ────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  RESULT")
    print(f"{'='*60}")
    print(f"  Success      : {result.success}")
    print(f"  Final state  : {result.final_state.value}")
    print(f"  Adapter      : {result.adapter_name}")
    if result.error:
        print(f"  Error        : {result.error}")
    if result.screenshot_path:
        print(f"  Screenshot   : {result.screenshot_path}")
    if result.meta:
        shots = result.meta.get("screenshots", [])
        if shots:
            print(f"  All shots    : {len(shots)} files")
            for s in shots[-3:]:   # show last 3
                print(f"    {s}")
    print(f"{'='*60}\n")

    if result.success:
        print("✔  Application recorded as SUCCESS in DB.")
    elif result.final_state.value == "human_intervention":
        print("⚠  Paused at HUMAN_INTERVENTION — check WhatsApp for instructions.")
        print("   Job stays 'approved' in DB — send DONE via WhatsApp to resume.")
    else:
        print("✘  Application FAILED — see logs/ for details.")


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("JobTracker — Amazon Apply End-to-End Test")
    print("  (auto_submit=False by default — won't click final Submit)")
    print("  Pass --submit to actually submit the application.\n")

    url = _get_url()
    print(f"\nTarget URL: {url}")

    _confirm_adapter(url)

    job_hash, company, title = _insert_or_get_job(url)
    _run(job_hash, company, title, url)
