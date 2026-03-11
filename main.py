"""JobTracker — Autonomous job-hunting agent."""

import sys
import io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import asyncio
import click
import yaml
from dotenv import load_dotenv
from loguru import logger
from pathlib import Path

from db.database import init_db, get_session
from db.models import Job

# Configure logging
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger.add(LOG_DIR / "agent.log", rotation="10 MB", retention="30 days")

load_dotenv()


def load_profile() -> dict:
    with open(Path(__file__).parent / "config" / "profile.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@click.group()
def cli():
    """JobTracker — Autonomous job-hunting agent."""
    init_db()


@cli.command()
def scan():
    """Scan hiremetech.com + LinkedIn for new jobs, score, and notify via WhatsApp."""
    from scanners.hiremetech import scrape_hiremetech
    from scanners.linkedin import scrape_linkedin
    from core.analyzer import score_job, should_keep
    from core.notifier import send_job_cards
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
        passing = []

        for job_data in jobs:
            existing = session.query(Job).filter(Job.job_id == job_data["job_id"]).first()
            if existing:
                continue

            try:
                result = await score_job(job_data, profile)
            except Exception as e:
                logger.error(f"Score failed {job_data['job_id']}: {e}")
                continue

            db_job = Job(
                job_id=job_data["job_id"],
                title=job_data["title"],
                company=job_data["company"],
                location=job_data.get("location"),
                description=job_data.get("description"),
                apply_url=job_data.get("apply_url"),
                date_posted=job_data.get("date_posted"),
                salary=job_data.get("salary"),
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
            new_count += 1

            if should_keep(result):
                enriched = {**job_data, **result}
                passing.append(enriched)
                db_job.status = "notified"

        session.commit()
        session.close()

        click.echo(f"New jobs stored: {new_count}")
        click.echo(f"Matching (student/junior, score>=7): {len(passing)}")

        if passing:
            sent, failed = send_job_cards(passing)
            click.echo(f"WhatsApp: {sent} sent, {failed} failed")
        else:
            click.echo("No new matching jobs to notify.")

    asyncio.run(run())


@cli.command("list")
def list_jobs():
    """Show scored jobs in a table."""
    from rich.console import Console
    from rich.table import Table

    session = get_session()
    jobs = session.query(Job).filter(Job.score >= 7).order_by(Job.score.desc()).all()
    session.close()

    if not jobs:
        click.echo("No matching jobs found yet. Run 'scan' first.")
        return

    console = Console(file=sys.stdout)
    table = Table(title="Matching Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Company", style="green")
    table.add_column("Title")
    table.add_column("Score", justify="right", style="magenta")
    table.add_column("Level")
    table.add_column("Status")

    for job in jobs:
        table.add_row(job.job_id, job.company, job.title, str(job.score), job.role_type or "?", job.status)

    console.print(table)


@cli.command()
@click.argument("job_id", required=False)
@click.option("--all", "apply_all", is_flag=True, help="Apply to all approved jobs")
def apply(job_id, apply_all):
    """Apply to a specific job or all approved jobs."""
    if not job_id and not apply_all:
        click.echo("Provide a job_id or use --all")
        return
    # Phase 2 — not yet implemented
    click.echo("Auto-apply not yet implemented (Phase 2).")


@cli.command()
def status():
    """Show stats dashboard."""
    from sqlalchemy import func

    session = get_session()
    total = session.query(Job).count()
    by_status = dict(
        session.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    )
    session.close()

    click.echo("JobTracker Status")
    click.echo(f"  Total jobs found: {total}")
    status_labels = {
        "new": "New", "scored": "Scored", "notified": "Notified",
        "approved": "Approved", "applying": "Applying",
        "applied": "Applied", "failed": "Failed",
    }
    for s, count in by_status.items():
        click.echo(f"  {status_labels.get(s, s)}: {count}")


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
def schedule():
    """Start the scheduler — scans every 12 hours + runs webhook."""
    import os
    import threading
    from apscheduler.schedulers.blocking import BlockingScheduler
    from webhook import app

    def run_scan():
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(scan)
        logger.info(f"Scheduled scan output:\n{result.output}")

    scheduler = BlockingScheduler()
    scheduler.add_job(run_scan, "interval", hours=12, id="scan_job")
    scheduler.add_job(run_scan, "date", id="scan_immediate")  # run once on startup

    port = int(os.environ.get("WEBHOOK_PORT", 5000))
    webhook_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True,
    )
    webhook_thread.start()
    click.echo(f"Scheduler started. Webhook on port {port}. Scanning every 12 hours.")
    click.echo("Expose webhook: ngrok http {port}")
    scheduler.start()


if __name__ == "__main__":
    cli()
