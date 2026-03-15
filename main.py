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

load_dotenv()

# Secure logging — must run before any other module emits log records.
# Replaces the default loguru handler with a redacting filter on all sinks.
from core.log_utils import setup_secure_logging
setup_secure_logging(log_dir=Path(__file__).parent / "logs")


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
            job_card = {**job_data, **result, "job_hash": job_hash}
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
    """Execute the apply flow for a single job via the ApplyOrchestrator."""
    from core.orchestrator import run_application
    from datetime import datetime, timezone

    # Mark as in-progress immediately so the dashboard reflects activity
    suggested_job.responded_at = datetime.now(timezone.utc)
    session.commit()

    result = run_application(suggested_job, auto_submit=auto_submit)

    if result.success:
        click.echo(f"[DB] Application recorded: SUCCESS (adapter={result.adapter_name})")
        click.echo(f"\nSuccessfully applied to {suggested_job.company} — {suggested_job.title}")
    else:
        click.echo(
            f"[DB] Application recorded: FAILED — {result.error} "
            f"(state={result.final_state.value})"
        )
        click.echo(f"\nFailed to apply: {result.error}")

    if result.screenshot_path:
        click.echo(f"\nScreenshot: {result.screenshot_path}")


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
@click.option("--scan-hours", default=6, show_default=True,
              help="How often to scan for new jobs (hours).")
def schedule(scan_hours):
    """Start the full autonomous agent: scheduler + webhook server.

    \b
    Runs:
      - Job scan every N hours (default 6) with 2h smart cooldown
      - Expiry check + skipped-job re-notification every hour
      - Inbound WhatsApp webhook on WEBHOOK_PORT (default 5000)
    """
    import os
    import threading
    from core.scheduler import start_scheduler
    from webhook import app

    scheduler = start_scheduler(scan_interval_hours=scan_hours)

    port = int(os.environ.get("WEBHOOK_PORT", 5000))
    webhook_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True,
    )
    webhook_thread.start()

    click.echo(f"JobTracker agent running.")
    click.echo(f"  Scan interval : every {scan_hours}h (with 2h cooldown)")
    click.echo(f"  Expiry/re-notify: every 1h")
    click.echo(f"  Webhook       : http://0.0.0.0:{port}/webhook")
    click.echo(f"  Expose via    : ngrok http {port}")
    click.echo("Press Ctrl+C to stop.")

    try:
        # Keep main thread alive
        import time
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        from core.scheduler import stop_scheduler
        stop_scheduler()
        click.echo("Scheduler stopped.")


@cli.command("linkedin-login")
def linkedin_login():
    """Open a browser to log in to LinkedIn manually — no password stored."""
    from scanners.linkedin import interactive_login

    click.echo("Opening browser for LinkedIn login...")
    click.echo("Log in with your credentials in the browser window.")
    click.echo("Your session will be saved automatically — no password is stored on disk.")

    success = asyncio.run(interactive_login())
    if success:
        click.echo("LinkedIn session saved successfully! You can now run scans.")
    else:
        click.echo("Login failed or timed out. Please try again.")


if __name__ == "__main__":
    cli()
