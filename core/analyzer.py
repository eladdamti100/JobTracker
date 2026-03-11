"""Claude-powered job scoring and summarization against candidate profile."""

import json
import os
import anthropic
from loguru import logger


SYSTEM_PROMPT = """You are a job-matching assistant for a software engineering student.
Evaluate job listings and return structured JSON only — no markdown, no text outside JSON."""

SCORE_PROMPT = """Candidate profile:
{profile_summary}

Job listing:
- Title: {title}
- Company: {company}
- Location: {location}
- Description: {description}
- Salary: {salary}

Analyze this job and respond with JSON only:
{{
  "score": <integer 1-10>,
  "level": "<student|junior|senior>",
  "role_type": "<Backend|Frontend|Full-Stack|DevOps|Systems|Data|Other>",
  "is_student_position": <true|false>,
  "tech_stack_match": ["<tech1>", "<tech2>"],
  "apply_strategy": "<easy_apply|external_form>",
  "role_summary": "<2-3 lines in Hebrew describing what the candidate will do in this role>",
  "requirements_summary": "<2-3 lines in Hebrew listing the key requirements>"
}}

Level classification:
- "student": explicitly for students, interns, or requires 0 years of experience
- "junior": 0-2 years of experience required, entry-level, graduate welcome
- "senior": 3+ years of experience required, or explicitly senior/lead/staff

Score guide:
- 9-10: Perfect match (student/junior role, strong skill overlap)
- 7-8: Good match (relevant role, most skills match)
- 5-6: Partial match (some skills match, but mismatch on level or domain)
- 1-4: Poor match (wrong domain, too senior, or weak skill overlap)"""

# Prompt for jobs from WhatsApp student groups (more generous scoring)
WA_SCORE_PROMPT = """This job was shared in a WhatsApp group for software engineering students in Israel.
Score it for a 3rd-year software engineering student at Bar Ilan looking for student/intern/junior positions.

Candidate profile:
{profile_summary}

Job listing:
- Title: {title}
- Company: {company}
- Location: {location}
- Description: {description}
- Salary: {salary}

RELEVANT roles (score 7-10): Backend, Frontend, Full-Stack, DevOps, Embedded, Mobile, QA Automation, Systems Programming, Cloud, Data Engineering
PARTIALLY RELEVANT (score 5-6): IT support, Technical PM, Data Analyst with coding
NOT RELEVANT (score 1-3): Non-tech (sales, marketing, design, product without code), Senior 3+ years, Hardware engineering, Data Science / ML Research (unless coding-heavy)

Respond with JSON only:
{{
  "score": <integer 1-10>,
  "level": "<student|junior|senior>",
  "role_type": "<Backend|Frontend|Full-Stack|DevOps|Embedded|QA|Systems|Data|Other>",
  "is_student_position": <true|false>,
  "tech_stack_match": ["<tech1>", "<tech2>"],
  "apply_strategy": "<easy_apply|external_form>",
  "role_summary": "<2-3 lines in Hebrew describing what the candidate will do in this role>",
  "requirements_summary": "<2-3 lines in Hebrew listing the key requirements>"
}}

Level classification:
- "student": explicitly for students, interns, or requires 0 years of experience
- "junior": 0-2 years experience, entry-level, graduate welcome
- "senior": 3+ years required, or explicitly senior/lead/staff"""


def _build_profile_summary(profile: dict) -> str:
    strong = ", ".join(profile.get("skills", {}).get("strong", []))
    web = ", ".join(profile.get("skills", {}).get("web", []))
    other = ", ".join(profile.get("skills", {}).get("other", []))
    roles = ", ".join(profile.get("seeking", {}).get("roles", [])) if isinstance(profile.get("seeking"), dict) else ""
    edu = profile.get("education", {})
    return (
        f"Name: {profile.get('name')}, {edu.get('year')} {edu.get('degree')} "
        f"at {edu.get('university')}, GPA {edu.get('gpa')}.\n"
        f"Seeking: student/intern positions in {roles}.\n"
        f"Strong skills: {strong}.\n"
        f"Web skills: {web}.\n"
        f"Other: {other}.\n"
        f"Military background: IDF C4I — Linux, DevOps, Docker."
    )


def should_keep(result: dict) -> bool:
    """Return True if this job should be sent.

    Thresholds:
      - student level  → score >= 6
      - junior level   → score >= 7
    """
    level = result.get("level", "senior")
    score = result.get("score", 0)
    if level not in ("student", "junior"):
        return False
    threshold = 6 if level == "student" else 7
    return score >= threshold


async def score_job(job: dict, profile: dict) -> dict:
    """Score and summarize a job listing against the candidate profile using Claude.

    Uses WA_SCORE_PROMPT for WhatsApp-sourced jobs (more generous scoring),
    and SCORE_PROMPT for hiremetech jobs.

    Returns dict with keys:
        score, level, role_type, is_student_position,
        tech_stack_match, apply_strategy, role_summary, requirements_summary
    """
    logger.info(f"Scoring: {job.get('title')} at {job.get('company')}")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    profile_summary = _build_profile_summary(profile)

    # Use generous student-group prompt for WhatsApp and LinkedIn sourced jobs
    source = job.get("source", "")
    job_id = job.get("job_id", "")
    is_student_source = (
        source.startswith("WhatsApp")
        or source == "LinkedIn"
        or job_id.startswith("WA-")
        or job_id.startswith("LI-")
    )
    template = WA_SCORE_PROMPT if is_student_source else SCORE_PROMPT

    prompt = template.format(
        profile_summary=profile_summary,
        title=job.get("title", ""),
        company=job.get("company", ""),
        location=job.get("location", ""),
        description=job.get("description", "")[:2000],
        salary=job.get("salary") or "Not specified",
    )

    for attempt in range(2):
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        if not raw:
            logger.warning(f"Empty Claude response (attempt {attempt + 1}) for {job.get('company')}")
            if attempt == 0:
                import time; time.sleep(2)
                continue
            raise ValueError("Claude returned empty response after retry")

        try:
            result = json.loads(raw)
            logger.info(f"Score: {result['score']}/10 level={result.get('level')} — {job.get('company')}")
            return result
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse error (attempt {attempt + 1}): {e}\nRaw: {raw[:200]}")
            if attempt == 0:
                import time; time.sleep(2)
                continue
            raise

    raise ValueError("Claude scoring failed after retry")
