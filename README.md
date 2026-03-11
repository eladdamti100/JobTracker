# JobTracker

Autonomous job-hunting agent that scans Israeli tech job sites, scores listings against your profile using Claude AI, notifies you via WhatsApp, and auto-applies on command.

## Features

- **Phase 1 — Scan & Notify**: Scrapes hiremetech.com every 12 hours, scores jobs with Claude, sends WhatsApp summaries
- **Phase 2 — Auto Apply**: Reply "APPLY {job_id}" on WhatsApp or use CLI to auto-fill and submit applications

## Setup

```bash
# Clone and install
git clone https://github.com/eladdamti100/JobTracker.git
cd JobTracker
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# Configure
cp .env.example .env
# Edit .env with your API keys
# Place your CV at data/cv.pdf
```

## Usage

```bash
python main.py scan              # Scan hiremetech now
python main.py list              # Show pending jobs
python main.py apply <job_id>    # Apply to a specific job
python main.py apply --all       # Apply to all approved jobs
python main.py status            # Stats dashboard
```

## Tech Stack

Python 3.11+ | Playwright | Claude API | Twilio WhatsApp | SQLite | APScheduler
