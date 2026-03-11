"""
Real scan test: scrape hiremetech → Claude score → WhatsApp notify.
Run: python test_real_scan.py
"""

import sys, io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import asyncio
import yaml
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box

from scanners.hiremetech import scrape_hiremetech
from core.analyzer import score_job
from core.notifier import send_whatsapp_jobs

load_dotenv()
console = Console(file=sys.stdout, highlight=False)


def load_profile() -> dict:
    with open(Path(__file__).parent / "config" / "profile.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run():
    console.rule("[bold blue]JobTracker — Real Scan")

    # Step 1: Scrape
    console.print("\n[cyan]Step 1: Scraping hiremetech.com...[/]")
    jobs = await scrape_hiremetech(max_jobs=100)
    console.print(f"[green]Scraped {len(jobs)} unique jobs[/]\n")

    if not jobs:
        console.print("[red]No jobs found. Aborting.[/]")
        return

    # Show sample of what was scraped
    console.print("[dim]Sample (first 5 scraped):[/]")
    for j in jobs[:5]:
        console.print(f"  [{j['job_level']}] {j['company']} — {j['title']} | {j['location']}")

    # Step 2: Score with Claude
    console.print(f"\n[cyan]Step 2: Scoring {len(jobs)} jobs with Claude...[/]")
    profile = load_profile()
    scored = []
    passing = []

    for i, job in enumerate(jobs):
        try:
            result = await score_job(job, profile)
            enriched = {**job, **result}
            scored.append(enriched)
            score = result["score"]
            mark = "[green]✅[/]" if score >= 7 else "[red]❌[/]"
            console.print(
                f"  {mark} [{i+1}/{len(jobs)}] {job['company']} — {job['title'][:45]} "
                f"→ {score}/10"
            )
            if score >= 7:
                passing.append(enriched)
        except Exception as e:
            console.print(f"  [yellow]SKIP {job['job_id']}: {e}[/]")

    console.print(f"\n[bold]Passing (≥7): {len(passing)}/{len(scored)} jobs[/]")

    if not passing:
        console.print("[yellow]No jobs passed threshold. Done.[/]")
        return

    # Step 3: Results table
    table = Table(title=f"Top Matches from hiremetech.com", box=box.ROUNDED)
    table.add_column("Company", style="green", max_width=20)
    table.add_column("Title", max_width=35)
    table.add_column("Score", justify="center", style="magenta", width=7)
    table.add_column("Role", width=12)
    table.add_column("Student?", justify="center", width=9)
    table.add_column("Location", max_width=18)

    for j in sorted(passing, key=lambda x: -x["score"]):
        student = "[green]✓[/]" if j.get("is_student_position") else "[red]✗[/]"
        table.add_row(
            j["company"], j["title"], str(j["score"]),
            j.get("role_type", "?"), student, j.get("location", "")
        )

    console.print()
    console.print(table)

    # Step 4: WhatsApp
    console.rule("[bold green]Step 3: Sending WhatsApp")
    top = sorted(passing, key=lambda x: -x["score"])[:10]
    console.print(f"\n[dim]Sending {len(top)} jobs to WhatsApp (auto-chunked)...[/]")

    ok = send_whatsapp_jobs(top)
    if ok:
        console.print("[bold green]✅ WhatsApp sent! Check your phone.[/]")
    else:
        console.print("[bold red]❌ WhatsApp failed.[/]")


if __name__ == "__main__":
    asyncio.run(run())
