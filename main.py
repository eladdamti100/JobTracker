"""JobTracker — Autonomous job-hunting agent."""

import click
from dotenv import load_dotenv
from loguru import logger
from pathlib import Path

from db.database import init_db

# Configure logging
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger.add(LOG_DIR / "agent.log", rotation="10 MB", retention="30 days")

load_dotenv()


@click.group()
def cli():
    """JobTracker — Autonomous job-hunting agent."""
    init_db()


@cli.command()
def scan():
    """Scan hiremetech.com for new jobs."""
    click.echo("🔍 Scanning hiremetech.com...")
    # TODO: Wire up scanner → analyzer → notifier pipeline
    click.echo("Not yet implemented.")


@cli.command("list")
def list_jobs():
    """Show pending scored jobs."""
    from rich.console import Console
    from rich.table import Table
    from db.database import get_session
    from db.models import Job

    session = get_session()
    jobs = session.query(Job).filter(Job.score >= 7).order_by(Job.score.desc()).all()

    if not jobs:
        click.echo("No matching jobs found yet. Run 'scan' first.")
        return

    console = Console()
    table = Table(title="Matching Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Company", style="green")
    table.add_column("Title")
    table.add_column("Score", justify="right", style="magenta")
    table.add_column("Status")

    for job in jobs:
        table.add_row(job.job_id, job.company, job.title, str(job.score), job.status)

    console.print(table)
    session.close()


@cli.command()
@click.argument("job_id", required=False)
@click.option("--all", "apply_all", is_flag=True, help="Apply to all approved jobs")
def apply(job_id, apply_all):
    """Apply to a specific job or all approved jobs."""
    if not job_id and not apply_all:
        click.echo("Provide a job_id or use --all")
        return
    # TODO: Wire up applicator
    click.echo("Not yet implemented.")


@cli.command()
def status():
    """Show stats dashboard."""
    from db.database import get_session
    from db.models import Job
    from sqlalchemy import func

    session = get_session()
    total = session.query(Job).count()
    by_status = dict(
        session.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    )

    click.echo(f"📊 JobTracker Status")
    click.echo(f"   Total jobs found: {total}")
    for s, count in by_status.items():
        click.echo(f"   {s}: {count}")
    session.close()


if __name__ == "__main__":
    cli()
