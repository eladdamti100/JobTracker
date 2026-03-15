"""Central configuration — loaded once at startup from environment variables.

All model names, timeouts, and feature flags live here.
Never import this before load_dotenv() has been called.
"""

import os


def _require(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        raise EnvironmentError(f"Required env var '{key}' is not set.")
    return val


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ── AI models ──────────────────────────────────────────────────────────────────
# Override any of these via environment variables to switch providers/models
# without touching code.

# Text model for job scoring, summarisation, field suggestions
PLANNER_MODEL: str = _get(
    "PLANNER_MODEL",
    "llama-3.3-70b-versatile",
)

# Vision model for screenshot-based page-state detection
VISION_MODEL: str = _get(
    "VISION_MODEL",
    "meta-llama/llama-4-scout-17b-16e-instruct",
)

# Lightweight fallback when the primary model is overloaded
FALLBACK_MODEL: str = _get(
    "FALLBACK_MODEL",
    "llama-3.1-8b-instant",
)

# ── Groq / OpenAI-compatible base URL ──────────────────────────────────────────
GROQ_API_KEY: str = _get("GROQ_API_KEY")
GROQ_BASE_URL: str = _get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# ── Applicator behaviour ───────────────────────────────────────────────────────
APPLY_MAX_STEPS: int = int(_get("APPLY_MAX_STEPS", "60"))
APPLY_STEP_TIMEOUT_MS: int = int(_get("APPLY_STEP_TIMEOUT_MS", "15000"))
APPLY_SCREENSHOT_ON_EVERY_STEP: bool = _get(
    "APPLY_SCREENSHOT_ON_EVERY_STEP", "false"
).lower() == "true"

# Maximum login attempts per platform before giving up (avoid account lockouts)
MAX_LOGIN_ATTEMPTS: int = int(_get("MAX_LOGIN_ATTEMPTS", "2"))

# How long to wait for a verification email (seconds)
EMAIL_VERIFY_TIMEOUT: int = int(_get("EMAIL_VERIFY_TIMEOUT", "120"))

# ── Paths ──────────────────────────────────────────────────────────────────────
import pathlib  # noqa: E402 — intentional late import to keep top clean

_ROOT = pathlib.Path(__file__).parent.parent

SCREENSHOTS_DIR: pathlib.Path = _ROOT / "data" / "screenshots"
DEFAULT_ANSWERS_PATH: pathlib.Path = _ROOT / "data" / "default_answers.yaml"
CV_PATH: pathlib.Path = _ROOT / "data" / "CV Resume.pdf"
DB_PATH: pathlib.Path = _ROOT / "data" / "jobtracker.db"
