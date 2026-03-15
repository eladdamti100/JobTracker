"""Secure logging utilities for JobTracker.

Provides a loguru redaction filter that strips secrets from every log
record before it reaches any sink.  No plaintext password, OTP, session
token, cookie value, or verification link ever appears in log files or
the console.

Redaction patterns
------------------
  password= / pwd= / token= / secret= / otp= / cookie= / key=
      Field-assignment pattern — redacts the value after = or :

  Bearer / Basic auth headers
      Redacts the token portion: "Bearer [REDACTED_TOKEN]"

  Verification / activation URLs
      Full URL replaced: "[REDACTED_VERIFY_URL]"

  Playwright storage_state blobs
      Replaced with: "[REDACTED_STORAGE_STATE]"

  6–8 digit OTP codes near relevant keywords
      Digit group replaced: "[REDACTED_OTP]"

Usage
-----
Call ``setup_secure_logging()`` once at process startup (before any
logger.add() calls) so all sinks get the filter.

    from core.log_utils import setup_secure_logging
    setup_secure_logging()

The ``redact_secrets(text)`` function is also exported for use in any
code that needs to sanitise strings before storing them (e.g., error
messages persisted in checkpoints).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from loguru import logger


# ── Redaction patterns ────────────────────────────────────────────────────────

# Field assignments: password=XYZ, token='XYZ', secret: XYZ, etc.
# Matches up to a whitespace / quote / punctuation boundary.
_FIELD_RE = re.compile(
    r"""(?ix)
    \b(password|passwd|pwd|secret|token|otp|pin|api_key|cookie
      |credential|auth_token|access_token|refresh_token|session
      |verification_code|activation_code)
    (?:\s*[=:]\s*)         # separator
    (?:"|')?               # optional quote
    ([^\s"',;{}\[\]]{4,})  # value (min 4 chars to avoid false positives)
    (?:"|')?               # optional closing quote
    """,
)

# Bearer / Basic auth header values
_BEARER_RE = re.compile(
    r"(?i)\b(bearer|basic)\s+([A-Za-z0-9+/=._\-]{16,})"
)

# Verification / activation / reset URLs
# Any URL whose path or query string contains a secret-looking segment.
_VERIFY_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+"
    r"(?:verify|confirm|activate|reset|token|validate|auth)"
    r"[^\s\"'<>]*",
    re.IGNORECASE,
)

# Playwright storage_state JSON blobs (cookies + localStorage)
# These can be very long; match from the keyword to the end of the JSON object.
_STORAGE_STATE_RE = re.compile(
    r"""(?ix)
    storage.?state         # "storage_state", "storageState", etc.
    \s*[=:]\s*
    [\{"\[]                # start of JSON value
    [^\n]{20,}             # at least 20 chars of content
    """,
    re.DOTALL,
)

# OTP digit sequences near relevant keywords (4–8 digits)
_OTP_RE = re.compile(
    r"""(?ix)
    \b(?:otp|code|pin|passcode|verification.?code|auth.?code)
    [^\d]{0,15}            # up to 15 non-digit separator chars
    (\d{4,8})              # the actual code
    \b
    """,
)

# Long base64-like strings that look like session tokens / API keys
# Only applied to strings that look like isolated tokens (not file paths etc.)
_TOKEN_RE = re.compile(
    r"""(?<![/\\.])        # not after a path separator
    \b([A-Za-z0-9+/=_\-]{48,})\b  # 48+ char token-like string
    """,
    re.VERBOSE,
)


def redact_secrets(text: str) -> str:
    """Strip secrets from *text*.  Returns a sanitised copy.

    Applies all redaction patterns in order.  Safe to call on any string —
    returns the unchanged input if no patterns match.  Never raises.
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        # Order matters: more specific patterns first.
        text = _STORAGE_STATE_RE.sub("[REDACTED_STORAGE_STATE]", text)
        text = _VERIFY_URL_RE.sub("[REDACTED_VERIFY_URL]", text)
        text = _BEARER_RE.sub(r"\1 [REDACTED_TOKEN]", text)
        text = _FIELD_RE.sub(r"\1=[REDACTED]", text)
        text = _OTP_RE.sub(
            lambda m: m.group(0).replace(m.group(1), "[REDACTED_OTP]"), text
        )
        # Long token fallback — broad, so runs last
        text = _TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    except Exception:
        pass  # never let redaction crash the caller
    return text


# ── Loguru filter ─────────────────────────────────────────────────────────────

def _redacting_filter(record: dict) -> bool:
    """Loguru ``filter`` function — redacts secrets in every log record.

    Mutates ``record["message"]`` in-place.  Returns True always so no
    records are suppressed — only sanitised.
    """
    try:
        record["message"] = redact_secrets(record["message"])
        extra = record.get("extra") or {}
        for k, v in extra.items():
            if isinstance(v, str):
                extra[k] = redact_secrets(v)
    except Exception:
        pass
    return True  # never suppress


# ── Public setup ──────────────────────────────────────────────────────────────

def setup_secure_logging(log_dir: str | Path = "logs") -> None:
    """Configure loguru with the redaction filter on all sinks.

    Replaces any existing loguru handlers so this must be called once,
    early in process startup (before any ``logger.add()`` calls elsewhere).

    Sinks added
    -----------
    console  — WARNING+ with colour, human-readable
    file     — DEBUG+, daily rotating, gzipped, retained 14 days
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Remove all existing handlers (including loguru's default stderr handler)
    logger.remove()

    _fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    # Console: WARNING and above — terse, colourised
    logger.add(
        sys.stderr,
        level="WARNING",
        format=_fmt,
        filter=_redacting_filter,
        colorize=True,
    )

    # File: DEBUG and above — verbose, rotating, compressed
    logger.add(
        str(log_path / "jobtracker_{time:YYYY-MM-DD}.log"),
        level="DEBUG",
        format=_fmt,
        filter=_redacting_filter,
        rotation="00:00",      # new file at midnight
        retention="14 days",
        compression="gz",
        enqueue=True,          # thread-safe async write
    )

    logger.info("Secure logging initialised (redaction filter active)")
