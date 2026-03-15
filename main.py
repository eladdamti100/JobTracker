"""JobTracker — Autonomous job-hunting agent."""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import click
import yaml
from dotenv import load_dotenv
from loguru import logger
from pathlib import Path

from db.database import init_db, get_session, is_duplicate
from db.models import SuggestedJob, Application, make_job_hash

# Configure logging
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger.add(LOG_DIR / "agent.log", rotation="10 MB", retention="30 days")

load_dotenv()


def load_profile() -> dict:
    with open(Path(__file__).parent / "config" / "profile.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _infer_source(job_id: str) -> str:
    if job_id.startswith("HMT-"):
        return "HireMeTech"
    if job_id.startswith("LI-"):
        return "LinkedIn"
    if job_id.startswith("WA-"):
        return "WhatsApp"
    return "Unknown"


@click.group()
def cli():
    """JobTracker — Autonomous job-hunting agent."""
    init_db()


@cli.command()
def scan():
    """Scan hiremetech.com + LinkedIn for new jobs, score, and suggest via WhatsApp."""
    from scanners.hiremetech import scrape_hiremetech
    from scanners.linkedin import scrape_linkedin
    from core.analyzer import score_job, should_keep
    from core.notifier import send_suggestion
    from datetime import datetime, timezone, timedelta

    async def run():
        profile = load_profile()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        def is_recent(job):
            d = job.get("date_posted")
            if not d:
                return False
            try:
                return datetime.fromisoformat(str(d)).replace(tzinfo=timezone.utc) >= cutoff
            except (ValueError, TypeError):
                return False

        # Hiremetech — filter to last 24h
        hmt_all = await scrape_hiremetech(max_jobs=200)
        hmt_recent = [j for j in hmt_all if is_recent(j)]
        click.echo(f"Hiremetech: {len(hmt_all)} scraped, {len(hmt_recent)} in last 24h")

        # LinkedIn — already filtered to 24h via f_TPR=r86400
        li_jobs = await scrape_linkedin(max_jobs=40)
        click.echo(f"LinkedIn:   {len(li_jobs)} scraped")

        jobs = hmt_recent + li_jobs
        click.echo(f"Total new candidates: {len(jobs)}")

        session = get_session()
        new_count = 0
        suggested_count = 0

        for job_data in jobs:
            # Calculate job_hash and check both tables for duplicates
            job_hash = make_job_hash(
                job_data.get("company", ""),
                job_data.get("title", ""),
                job_data.get("apply_url", ""),
            )

            if is_duplicate(job_hash):
                logger.debug(f"[SCAN] Skipping duplicate: {job_data.get('company')} — {job_data.get('title')}")
                continue

            try:
                result = await score_job(job_data, profile)
            except Exception as e:
                logger.error(f"Score failed {job_data.get('job_id', '?')}: {e}")
                continue

            source = _infer_source(job_data.get("job_id", ""))

            if not should_keep(result):
                logger.info(f"[SCAN] Below threshold (score={result.get('score')}, level={result.get('level')}): {job_data.get('title')}")
                continue

            # Insert into suggested_jobs
            suggested = SuggestedJob(
                job_hash=job_hash,
                company=job_data["company"],
                title=job_data["title"],
                source=source,
                apply_url=job_data.get("apply_url"),
                location=job_data.get("location"),
                description=job_data.get("description"),
                date_posted=job_data.get("date_posted"),
                salary=job_data.get("salary"),
                score=result["score"],
                reason=result.get("reason"),
                level=result.get("level"),
                role_type=result.get("role_type"),
                tech_stack_match=result.get("tech_stack_match"),
                is_student_position=int(result.get("is_student_position", False)),
                apply_strategy=result.get("apply_strategy"),
                role_summary=result.get("role_summary"),
                requirements_summary=result.get("requirements_summary"),
                status="suggested",
            )
            session.add(suggested)
            session.flush()  # Get ID before sending WhatsApp
            new_count += 1

            # Send WhatsApp suggestion card
            job_card = {**job_data, **result}
            ok = send_suggestion(job_card)
            if ok:
                suggested_count += 1
                logger.info(f"[SCAN] Suggested: {job_data['company']} — {job_data['title']} (score={result['score']})")
            else:
                logger.warning(f"[SCAN] WhatsApp send failed for: {job_data['company']} — {job_data['title']}")

        session.commit()
        session.close()

        click.echo(f"New suggestions: {new_count}")
        click.echo(f"WhatsApp cards sent: {suggested_count}")

    asyncio.run(run())


@cli.command("list")
def list_jobs():
    """Show pending suggested jobs."""
    from rich.console import Console
    from rich.table import Table

    session = get_session()
    jobs = (
        session.query(SuggestedJob)
        .filter(SuggestedJob.status == "suggested")
        .order_by(SuggestedJob.score.desc())
        .all()
    )
    session.close()

    if not jobs:
        click.echo("No pending suggestions. Run 'scan' first.")
        return

    console = Console(file=sys.stdout)
    table = Table(title="Pending Job Suggestions")
    table.add_column("Hash", style="cyan", max_width=8)
    table.add_column("Company", style="green")
    table.add_column("Title")
    table.add_column("Score", justify="right", style="magenta")
    table.add_column("Level")
    table.add_column("Source")
    table.add_column("Status")

    for job in jobs:
        table.add_row(
            job.job_hash[:8],
            job.company,
            job.title,
            f"{job.score:.1f}" if job.score else "?",
            job.level or "?",
            job.source or "?",
            job.status,
        )

    console.print(table)


@cli.command()
@click.argument("job_hash", required=False)
@click.option("--auto", "auto_mode", is_flag=True, help="Auto-apply to all approved jobs")
@click.option("--auto-submit", is_flag=True, help="Submit without manual confirmation")
def apply(job_hash, auto_mode, auto_submit):
    """Apply to a specific job or all approved jobs.

    \b
    Usage:
      python main.py apply abc12345        # Apply to one job by hash prefix
      python main.py apply --auto          # Auto-apply to all approved jobs
    """
    if not job_hash and not auto_mode:
        click.echo("Provide a job_hash (or prefix) or use --auto")
        return

    session = get_session()

    if auto_mode:
        jobs = (
            session.query(SuggestedJob)
            .filter(SuggestedJob.status == "approved")
            .order_by(SuggestedJob.score.desc())
            .all()
        )

        # Exclude LinkedIn Easy Apply
        jobs = [j for j in jobs if "linkedin.com" not in (j.apply_url or "").lower()]

        if not jobs:
            click.echo("No approved jobs for auto-apply.")
            click.echo("Approve jobs via WhatsApp (reply YES) or the dashboard.")
            session.close()
            return

        click.echo(f"Found {len(jobs)} approved jobs for auto-apply:\n")
        for j in jobs:
            click.echo(f"  {j.job_hash[:8]} | {j.company:20s} | {j.title:35s} | score={j.score}")
        click.echo()

        for i, job in enumerate(jobs, 1):
            click.echo(f"\n{'='*60}")
            click.echo(f"[{i}/{len(jobs)}] Applying to: {job.title} at {job.company}")
            click.echo(f"{'='*60}\n")

            _run_apply(session, job, auto_submit=True)

    else:
        # Find by hash prefix
        job = (
            session.query(SuggestedJob)
            .filter(SuggestedJob.job_hash.like(f"{job_hash}%"))
            .first()
        )
        if not job:
            click.echo(f"Job not found with hash prefix: {job_hash}")
            session.close()
            return

        if not job.apply_url:
            click.echo(f"No apply URL for job: {job.company} — {job.title}")
            session.close()
            return

        click.echo(f"Job: {job.title} at {job.company}")
        click.echo(f"URL: {job.apply_url}")
        click.echo(f"Score: {job.score} | Level: {job.level}\n")

        _run_apply(session, job, auto_submit=auto_submit)

    session.close()


def _run_apply(session, suggested_job: SuggestedJob, auto_submit: bool = False):
    """Execute the apply flow for a single job and record in applications table."""
    from core.applicator import apply_to_job
    from datetime import datetime, timezone

    # Mark suggested job as being applied
    suggested_job.status = "applied"
    suggested_job.responded_at = datetime.now(timezone.utc)
    session.commit()
    click.echo(f"[DB] SuggestedJob status -> applied")

    result = apply_to_job(
        job_id=suggested_job.job_hash[:8],
        apply_url=suggested_job.apply_url,
        job_title=suggested_job.title,
        company=suggested_job.company,
        job_description=suggested_job.description or "",
        auto_submit=auto_submit,
    )

    now = datetime.now(timezone.utc)

    # Create application record
    app_record = Application(
        job_hash=suggested_job.job_hash,
        company=suggested_job.company,
        title=suggested_job.title,
        source=suggested_job.source,
        apply_url=suggested_job.apply_url,
        applied_at=now,
        application_method="auto_apply",
    )

    if result["success"]:
        app_record.application_result = result.get("application_result", "success")
        app_record.status = "success"
        app_record.cover_letter_used = result.get("cover_letter")
        app_record.screenshot_path = result.get("screenshots", [None])[0] if result.get("screenshots") else None
        click.echo(f"[DB] Application recorded: SUCCESS")
        click.echo(f"\nSuccessfully applied to {suggested_job.company} — {suggested_job.title}")
    else:
        app_record.application_result = "failed"
        app_record.status = "failed"
        app_record.error_message = result.get("error", "Unknown error")
        click.echo(f"[DB] Application recorded: FAILED — {app_record.error_message}")
        click.echo(f"\nFailed to apply: {result.get('error')}")

    session.add(app_record)
    session.commit()

    # Print screenshot paths
    screenshots = result.get("screenshots", [])
    if screenshots:
        click.echo(f"\nScreenshots ({len(screenshots)}):")
        for s in screenshots:
            click.echo(f"  -> {s}")


@cli.command()
def status():
    """Show stats dashboard."""
    from sqlalchemy import func

    session = get_session()

    suggested_total = session.query(SuggestedJob).count()
    suggested_by_status = dict(
        session.query(SuggestedJob.status, func.count(SuggestedJob.id))
        .group_by(SuggestedJob.status).all()
    )

    app_total = session.query(Application).count()
    app_by_status = dict(
        session.query(Application.status, func.count(Application.id))
        .group_by(Application.status).all()
    )

    session.close()

    click.echo("JobTracker Status")
    click.echo(f"\n  Suggested Jobs: {suggested_total}")
    for s, count in suggested_by_status.items():
        click.echo(f"    {s}: {count}")

    click.echo(f"\n  Applications: {app_total}")
    for s, count in app_by_status.items():
        click.echo(f"    {s}: {count}")


@cli.command()
def webhook():
    """Start the inbound WhatsApp webhook server."""
    import os
    from webhook import app
    port = int(os.environ.get("WEBHOOK_PORT", 5000))
    click.echo(f"Starting webhook on http://0.0.0.0:{port}/webhook")
    click.echo("Expose via ngrok: ngrok http {port}")
    app.run(host="0.0.0.0", port=port, debug=False)


@cli.command()
@click.option("--port", default=5001, help="Port to run the API server on")
def api(port):
    """Start the REST API server for the dashboard."""
    from api import app as api_app
    click.echo(f"Starting REST API on http://0.0.0.0:{port}")
    click.echo("Dashboard: set NEXT_PUBLIC_API_URL=http://localhost:{port} in dashboard/.env.local")
    api_app.run(host="0.0.0.0", port=port, debug=False)


@cli.command()
def expire():
    """Expire old suggestions that got no response."""
    from core.expiry import expire_old_suggestions
    expired = expire_old_suggestions()
    click.echo(f"Expired {expired} suggestions.")


@cli.command()
def schedule():
    """Start the scheduler — scans every 12 hours + runs webhook + hourly expiry."""
    import os
    import threading
    from apscheduler.schedulers.blocking import BlockingScheduler
    from webhook import app
    from core.expiry import expire_old_suggestions

    def run_scan():
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(scan)
        logger.info(f"Scheduled scan output:\n{result.output}")

    def run_expiry():
        expired = expire_old_suggestions()
        if expired > 0:
            logger.info(f"Expiry check: {expired} suggestions expired")

    scheduler = BlockingScheduler()
    scheduler.add_job(run_scan, "interval", hours=12, id="scan_job")
    scheduler.add_job(run_scan, "date", id="scan_immediate")  # run once on startup
    scheduler.add_job(run_expiry, "interval", hours=1, id="expiry_job")

    port = int(os.environ.get("WEBHOOK_PORT", 5000))
    webhook_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True,
    )
    webhook_thread.start()
    click.echo(f"Scheduler started. Webhook on port {port}. Scanning every 12h. Expiry every 1h.")
    click.echo("Expose webhook: ngrok http {port}")
    scheduler.start()


if __name__ == "__main__":
    cli()
