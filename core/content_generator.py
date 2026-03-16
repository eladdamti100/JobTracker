"""AI-powered fallback for form fields not covered by default_answers.yaml.

When the static answer database has no value for a field, ContentGenerator
asks Groq to determine what to fill based on:
  - The exact field label, type, and available options (for select/radio)
  - The job title, company, and description
  - The candidate's structured project/experience database (candidate_projects.yaml)

Lifecycle: one ContentGenerator instance per apply_to_job() call.
Caching: results are cached per (normalized_label, job_hash) to avoid
         repeated API calls for the same field in multi-page forms.
Timeout: Groq calls time out after 15s and fall back to None gracefully.
"""

import concurrent.futures
import hashlib
import os
from pathlib import Path

import yaml
from loguru import logger
from openai import OpenAI

ROOT = Path(__file__).parent.parent
CANDIDATE_PROJECTS_PATH = ROOT / "data" / "candidate_projects.yaml"

# ── Module-level cache for candidate data (loaded once per process) ──────────
_candidate_data_cache: dict | None = None


def _get_candidate_data() -> dict:
    global _candidate_data_cache
    if _candidate_data_cache is None:
        if CANDIDATE_PROJECTS_PATH.exists():
            with open(CANDIDATE_PROJECTS_PATH, encoding="utf-8") as f:
                _candidate_data_cache = yaml.safe_load(f) or {}
            logger.debug("ContentGenerator: loaded candidate_projects.yaml")
        else:
            _candidate_data_cache = {}
            logger.warning(
                "ContentGenerator: candidate_projects.yaml not found — "
                "AI fallback will have limited context"
            )
    return _candidate_data_cache


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize(label: str) -> str:
    """Lowercase + strip for cache keys."""
    return label.lower().strip()


def _build_projects_block(data: dict) -> str:
    projects = data.get("projects", [])
    if not projects:
        return "No projects listed."
    lines = []
    for p in projects:
        name = p.get("name", "")
        tech = ", ".join(p.get("tech", []))
        desc = (p.get("description") or "").strip().replace("\n", " ")
        highlights = "; ".join(p.get("highlights", []))
        scale = p.get("scale", "")
        if name and name.startswith("TODO"):
            continue
        line = f"- {name}"
        if tech:
            line += f" ({tech})"
        if desc:
            line += f": {desc}"
        if highlights:
            line += f" Key points: {highlights}."
        if scale:
            line += f" [{scale}]"
        lines.append(line)
    return "\n".join(lines) if lines else "No projects listed."


def _build_skills_block(data: dict) -> str:
    skills = data.get("skills_context", {})
    if not skills:
        return ""
    return "\n".join(f"- {k}: {v}" for k, v in skills.items())


def _build_experiences_block(data: dict) -> str:
    experiences = data.get("experiences", [])
    if not experiences:
        return ""
    lines = []
    for e in experiences:
        role = e.get("role", "")
        company = e.get("company", "")
        period = e.get("period", "")
        desc = (e.get("description") or "").strip().replace("\n", " ")
        highlights = "; ".join(e.get("highlights", []))
        line = f"- {role} at {company} ({period}): {desc}"
        if highlights:
            line += f" Highlights: {highlights}."
        lines.append(line)
    return "\n".join(lines) if lines else ""


# ── Main class ────────────────────────────────────────────────────────────────

class ContentGenerator:
    """Generates form field values using Groq when the static YAML has no answer.

    Usage:
        gen = ContentGenerator(client, job_title, company, job_description)

        # In _fill_field, after lookup_answer returns "":
        value = gen.generate(field_label, field_type, options=["Yes", "No"])
    """

    def __init__(
        self,
        client: OpenAI,
        job_title: str,
        company: str,
        job_description: str,
    ):
        self._client = client
        self._job_title = job_title or "Software Engineering Position"
        self._company = company or "Unknown Company"
        self._job_description = (job_description or "")[:800]
        self._job_hash = hashlib.md5(
            f"{self._job_title}{self._company}".encode()
        ).hexdigest()[:8]
        self._cache: dict[str, str] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def generate(
        self,
        field_label: str,
        field_type: str,
        options: list[str] | None = None,
        timeout: float = 15.0,
    ) -> str | None:
        """Ask Groq what value to fill for this field.

        Returns a string value to fill, or None if generation fails/times out.
        Results are cached: same field label + same job → same response.

        Args:
            field_label: The form field label (e.g. "Years of experience in Python")
            field_type:  HTML field type ("text", "textarea", "select", "radio", etc.)
            options:     Available choices for select/radio fields
            timeout:     Max seconds to wait for Groq response
        """
        if not field_label:
            return None

        cache_key = f"{_normalize(field_label)}::{self._job_hash}"
        if cache_key in self._cache:
            logger.debug(f"ContentGenerator: cache hit for {field_label!r}")
            return self._cache[cache_key]

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self._call_groq, field_label, field_type, options or []
            )
            try:
                result = future.result(timeout=timeout)
                if result:
                    self._cache[cache_key] = result
                    logger.info(
                        f"ContentGenerator: generated value for {field_label!r} "
                        f"-> \"{result[:60]}{'...' if len(result) > 60 else ''}\""
                    )
                return result
            except concurrent.futures.TimeoutError:
                logger.warning(
                    f"ContentGenerator: timeout ({timeout}s) for field {field_label!r}"
                )
                return None
            except Exception as e:
                logger.warning(
                    f"ContentGenerator: failed for {field_label!r}: {e}"
                )
                return None

    # ── Private ───────────────────────────────────────────────────────────────

    def _call_groq(
        self,
        field_label: str,
        field_type: str,
        options: list[str],
    ) -> str | None:
        prompt = self._build_prompt(field_label, field_type, options)
        try:
            response = self._client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are helping fill out a job application form on behalf of a candidate. "
                            "Return ONLY the value to fill in — no explanation, no labels, no JSON, no quotes."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=200,
            )
            raw = response.choices[0].message.content or ""
            return raw.strip()
        except Exception as e:
            logger.error(f"ContentGenerator: Groq API error: {e}")
            return None

    def _build_prompt(
        self,
        field_label: str,
        field_type: str,
        options: list[str],
    ) -> str:
        data = _get_candidate_data()
        general_ctx = (data.get("general_context") or "").strip().replace("\n", " ")
        projects_block = _build_projects_block(data)
        skills_block = _build_skills_block(data)
        experiences_block = _build_experiences_block(data)

        # Field type hint
        if field_type == "textarea":
            format_hint = (
                "Write 2–5 sentences (50–120 words). "
                "Be specific — reference a real project or skill. "
                "Do NOT start with 'I am excited to' or 'As a passionate'."
            )
        elif field_type in ("select", "radio") and options:
            clean_opts = [o for o in options if o.strip() and o.lower() not in
                          ("select", "select...", "choose", "choose...", "--", "---", "please select")]
            opts_str = ", ".join(f'"{o}"' for o in clean_opts)
            format_hint = (
                f"Choose the single best option from: {opts_str}. "
                "Return ONLY the exact option text, nothing else."
            )
        elif field_type in ("text", "email", "tel", "url", "number"):
            format_hint = "Return a short, direct value (a few words or a number). No sentences."
        else:
            format_hint = "Return a concise, appropriate value."

        prompt_parts = [
            "You are filling out a job application form field on behalf of a candidate.",
            "",
            f"JOB: {self._job_title} at {self._company}",
        ]
        if self._job_description:
            prompt_parts.append(f"Job description excerpt: {self._job_description}")

        prompt_parts += [
            "",
            f"CANDIDATE: {general_ctx}",
        ]
        if experiences_block:
            prompt_parts += ["", "EXPERIENCE:", experiences_block]
        if projects_block:
            prompt_parts += ["", "PROJECTS:", projects_block]
        if skills_block:
            prompt_parts += ["", "SKILLS:", skills_block]

        prompt_parts += [
            "",
            f'FORM FIELD TO FILL: "{field_label}" (type: {field_type})',
            "",
            f"INSTRUCTIONS: {format_hint}",
            "Do NOT invent experience the candidate doesn't have.",
            "Return ONLY the answer value — no labels, no explanation.",
        ]

        return "\n".join(prompt_parts)
