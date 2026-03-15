# CLAUDE.md — JobTracker

## What is this project?

JobTracker is an autonomous job-hunting agent for software engineering students in Israel.
It scans job sources, scores them with Claude AI, sends suggestions via WhatsApp,
waits for user approval, and auto-applies using Playwright — all tracked on a dashboard.

## End-to-End Pipeline

```
 SCAN               SCORE              SUGGEST             DECIDE              APPLY
 ─────              ─────              ───────             ──────              ─────
 HireMeTech ─┐                      Save to DB         User replies        Playwright
 LinkedIn   ─┼─→ Claude AI ─→ ─→  WhatsApp card  ─→  via WhatsApp  ─→  fills forms
 WA Groups  ─┘   (1-10 score)     (YES/NO/SKIP)      YES → apply       → screenshot
                                                      NO  → reject      → save result
                                                      SKIP → snooze     → update DB
                                                                        → notify user
                                                                        → update dashboard
```

### Status lifecycle

`suggested` → `approved` (YES) → `applied` (auto-apply ran)
`suggested` → `rejected` (NO)
`suggested` → `skipped` (SKIP, re-suggest after 12h)
`suggested` → `expired` (no response within 24h)

## Architecture

### Python backend
| File | Role |
|------|------|
| `main.py` | CLI: scan, apply, webhook, api, schedule, expire |
| `api.py` | Flask REST API for dashboard (port 5001) |
| `webhook.py` | Twilio WhatsApp inbound handler (port 5000) |
| `core/analyzer.py` | Claude AI job scoring & summarization |
| `core/applicator.py` | Playwright form-filling engine (Claude Vision for field detection) |
| `core/notifier.py` | Twilio WhatsApp outbound messaging |
| `core/expiry.py` | Hourly job to expire stale suggestions |
| `scanners/hiremetech.py` | HireMeTech public API scraper |
| `scanners/linkedin.py` | LinkedIn Playwright scraper (session-based auth) |
| `scanners/whatsapp_bridge.py` | Flask bridge receiving URLs from WhatsApp group listener |
| `db/models.py` | SQLAlchemy models: SuggestedJob, Application |
| `db/database.py` | SQLite engine, session factory, dedup check |

### Next.js dashboard (`dashboard/`)
| Page | Purpose |
|------|---------|
| `/` | Stats overview (suggested, applied, rejected counts) |
| `/suggested` | Suggested jobs list with filters + approve/reject |
| `/suggested/[hash]` | Single job detail + scoring breakdown |
| `/applications` | Applications history with status tracking |

### Data files (gitignored — contain personal info)
| File | Purpose |
|------|---------|
| `config/profile.yaml` | Candidate profile for Claude scoring |
| `data/default_answers.yaml` | Answer database for form auto-fill |
| `data/CV Resume.pdf` | CV for upload fields |
| `data/linkedin_session.json` | Persistent LinkedIn session cookies |
| `data/jobtracker.db` | SQLite database |

## Key concepts

- **Job hash**: `MD5(company + title + apply_url)` — used for deduplication across all sources
- **Scoring thresholds**: student-level jobs ≥ 6, junior-level ≥ 7
- **Form filling**: 4-strategy cascade — direct mapping → fuzzy normalization → Claude suggestion → profile defaults
- **Claude Vision**: screenshots of application forms are sent to Claude to identify and classify fields
- **Background apply**: webhook and dashboard trigger auto-apply in background threads to avoid blocking

## External services

- **Claude API** (Anthropic) — job scoring + form field detection via Vision
- **Twilio** — WhatsApp messaging (inbound webhook + outbound notifications)
- **LinkedIn** — Playwright scraping with saved session cookies
- **HireMeTech** — public REST API

## Running the project

```bash
# Start webhook server (WhatsApp inbound)
python main.py webhook

# Start REST API (dashboard backend)
python main.py api

# Run scan + score + notify
python main.py scan

# Start scheduler (scan every 12h + expiry every 1h + webhook)
python main.py schedule

# Auto-apply to all approved jobs
python main.py apply --auto

# Start dashboard
cd dashboard && npm run dev
```

## Environment variables

Defined in `.env` (gitignored). See `.env.example` for template.
Required: `ANTHROPIC_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_WHATSAPP_FROM`, `MY_WHATSAPP_NUMBER`, `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD`.

## Tech stack

- **Python**: Flask, Playwright, SQLAlchemy, Anthropic SDK, Twilio, APScheduler, Loguru
- **Frontend**: Next.js 15, React 19, TypeScript, Tailwind CSS
- **Database**: SQLite
- **AI**: Claude (claude-sonnet-4-20250514) for scoring and Vision-based form analysis

## Conventions

- All dates in DB are ISO format (`YYYY-MM-DD`)
- WhatsApp commands are case-insensitive (YES, yes, Yes all work)
- Job sources: `"HireMeTech"`, `"LinkedIn"`, `"WhatsApp"`
- Logs go to `logs/` directory (gitignored)
- Screenshots saved to `data/screenshots/` (gitignored)
