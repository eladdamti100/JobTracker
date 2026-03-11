"""
Phase 1 end-to-end test: fake jobs → Claude scoring → WhatsApp notification.
Run: python test_phase1.py
"""

import sys
import io
# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import asyncio
import yaml
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger
from rich.console import Console
from rich.table import Table
from rich import box

from core.analyzer import score_job
from core.notifier import send_whatsapp, format_job_message

load_dotenv()

console = Console(file=sys.stdout, highlight=False)

FAKE_JOBS = [
    {
        "job_id": "TEST-001",
        "title": "Backend Node.js Developer — Student Position",
        "company": "FinTech Startup TLV",
        "location": "תל אביב",
        "description": (
            "We're looking for a student developer to join our backend team. "
            "You'll work with Node.js, Express, MongoDB, and REST APIs. "
            "Docker knowledge is a plus. Part-time, flexible hours for students."
        ),
        "apply_url": "https://example.com/apply/001",
        "date_posted": "2024-03-10",
        "salary": "40-50 ₪/שעה",
    },
    {
        "job_id": "TEST-002",
        "title": "Senior DevOps Engineer",
        "company": "BigCorp Israel",
        "location": "הרצליה",
        "description": (
            "5+ years of experience required. Expert-level Kubernetes, Terraform, "
            "AWS, CI/CD pipelines. Lead DevOps architect for a team of 20 engineers. "
            "Full-time position, senior level."
        ),
        "apply_url": "https://example.com/apply/002",
        "date_posted": "2024-03-09",
        "salary": "35,000-45,000 ₪/חודש",
    },
    {
        "job_id": "TEST-003",
        "title": "C++ Embedded Systems Intern",
        "company": "Rafael Advanced Defense Systems",
        "location": "חיפה",
        "description": (
            "Internship for 3rd or 4th year software engineering students. "
            "Develop embedded C++ modules for real-time systems. "
            "Multi-threading, performance optimization, Linux environment. "
            "Great opportunity to work on cutting-edge defense tech."
        ),
        "apply_url": "https://example.com/apply/003",
        "date_posted": "2024-03-10",
        "salary": "45 ₪/שעה",
    },
    {
        "job_id": "TEST-004",
        "title": "Marketing Manager — Digital",
        "company": "E-commerce Co",
        "location": "תל אביב",
        "description": (
            "Lead digital marketing campaigns, SEO, social media strategy, "
            "content creation, Google Ads, Facebook Ads. "
            "3+ years marketing experience required. Not a tech role."
        ),
        "apply_url": "https://example.com/apply/004",
        "date_posted": "2024-03-08",
        "salary": "15,000-20,000 ₪/חודש",
    },
    {
        "job_id": "TEST-005",
        "title": "Full-Stack React/Node.js Intern",
        "company": "StartupNation Labs",
        "location": "תל אביב / ריחני",
        "description": (
            "Join our product team as a full-stack intern. "
            "Build features using React, Node.js, Express, MongoDB. "
            "JWT authentication, REST API design. Docker for local dev. "
            "Perfect for 2nd-4th year CS/Software Engineering students. "
            "Hybrid work model, great learning environment."
        ),
        "apply_url": "https://example.com/apply/005",
        "date_posted": "2024-03-11",
        "salary": "45-55 ₪/שעה",
    },
    {
        "job_id": "TEST-006",
        "title": "Senior Data Scientist",
        "company": "AI Research Lab",
        "location": "רחובות",
        "description": (
            "PhD preferred. 4+ years experience in ML/DL, PyTorch, TensorFlow. "
            "Research publications in NLP or Computer Vision expected. "
            "Lead a team of 5 data scientists. Full-time senior position."
        ),
        "apply_url": "https://example.com/apply/006",
        "date_posted": "2024-03-07",
        "salary": "40,000-55,000 ₪/חודש",
    },
]


def load_profile() -> dict:
    profile_path = Path(__file__).parent / "config" / "profile.yaml"
    with open(profile_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run_test():
    console.rule("[bold blue]JobTracker — Phase 1 Test")
    console.print(f"\n[cyan]Testing {len(FAKE_JOBS)} fake jobs through Claude scoring...\n")

    profile = load_profile()
    scored_jobs = []

    # Score each job
    for job in FAKE_JOBS:
        try:
            result = await score_job(job, profile)
            enriched = {**job, **result}
            scored_jobs.append(enriched)
            status = "[green]✅ PASS[/]" if result["score"] >= 7 else "[red]❌ SKIP[/]"
            console.print(
                f"{status} {job['company']} — {job['title']} "
                f"→ [bold]{result['score']}/10[/] | {result['reason']}"
            )
        except Exception as e:
            console.print(f"[red]ERROR scoring {job['job_id']}: {e}[/]")

    # Filter to >= 7
    passing = [j for j in scored_jobs if j.get("score", 0) >= 7]

    # Results table
    console.print()
    table = Table(title="Scoring Results", box=box.ROUNDED)
    table.add_column("ID", style="cyan", width=10)
    table.add_column("Company", style="green")
    table.add_column("Title")
    table.add_column("Score", justify="center", style="magenta", width=7)
    table.add_column("Role Type", width=12)
    table.add_column("Student?", justify="center", width=9)
    table.add_column("Result", justify="center", width=8)

    for j in scored_jobs:
        score = j.get("score", 0)
        result_str = "[green]SEND[/]" if score >= 7 else "[red]SKIP[/]"
        student = "[green]✓[/]" if j.get("is_student_position") else "[red]✗[/]"
        table.add_row(
            j["job_id"], j["company"], j["title"][:40],
            str(score), j.get("role_type", "?"), student, result_str,
        )

    console.print(table)
    console.print(f"\n[bold]Passing (score ≥ 7): {len(passing)}/{len(FAKE_JOBS)} jobs[/]\n")

    if not passing:
        console.print("[yellow]No jobs passed the threshold. Check your Claude API key.[/]")
        return

    # Send WhatsApp
    console.rule("[bold green]Sending WhatsApp Notification")
    message = format_job_message(passing)
    console.print("\n[dim]Message preview:[/]")
    console.print(f"[dim]{message}[/]\n")

    success = send_whatsapp(message)
    if success:
        console.print("[bold green]✅ WhatsApp message sent! Check your phone.[/]")
    else:
        console.print("[bold red]❌ WhatsApp failed. Check Twilio credentials in .env[/]")


if __name__ == "__main__":
    asyncio.run(run_test())
