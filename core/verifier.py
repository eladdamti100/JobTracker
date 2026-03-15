"""Human-in-the-loop verification helpers for Stage 7.

Coordinates between the browser-side adapter, WhatsApp notifications, and the
DB-backed ConversationState to handle OTP codes and CAPTCHA/email-link
verification without ever logging sensitive values.

Public API
----------
request_otp_from_user(job_hash, company, platform_name, apply_url)
    Sends a WhatsApp message asking for the OTP/code, sets ConversationState
    to ``pending_otp``.

poll_for_otp(job_hash, timeout_seconds) -> str | None
    Blocks, polling ConversationState every few seconds.  Returns the code
    when available, None on timeout.  The code is NEVER written to any log.

request_human_intervention(job_hash, company, apply_url, reason)
    Sends a WhatsApp message asking the user to act manually (CAPTCHA,
    email verification link).  Sets ConversationState to
    ``pending_intervention`` so the webhook knows to handle the DONE reply.

clear_verification_state()
    Resets ConversationState back to idle.  Call after a successful OTP
    fill or after giving up on a timeout.

Constants
---------
VERIFY_TIMEOUT_SECONDS  -- how long poll_for_otp waits before giving up (300 s)
OTP_SELECTORS           -- CSS selector list for OTP input fields
CAPTCHA_SELECTORS       -- CSS selector list for CAPTCHA widgets
OTP_SUBMIT_TEXTS        -- ordered list of button labels to click after OTP fill
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from loguru import logger


# ── Tunables ──────────────────────────────────────────────────────────────────

VERIFY_TIMEOUT_SECONDS = 300   # 5 minutes — how long we wait for user OTP
_POLL_INTERVAL = 5             # seconds between DB polls


# ── Selectors ────────────────────────────────────────────────────────────────

# OTP / verification-code input fields (tried in order; first visible wins)
OTP_SELECTORS: list[str] = [
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[name*="code" i][type="text"]',
    'input[name*="code" i][type="number"]',
    'input[name*="verif" i]',
    'input[inputmode="numeric"][maxlength]',
    'input[placeholder*="code" i]',
    'input[placeholder*="otp" i]',
]

# CAPTCHA presence indicators
CAPTCHA_SELECTORS: list[str] = [
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[title*="captcha" i]',
    '.g-recaptcha',
    '.h-captcha',
    '[class*="captcha" i]',
    '[data-sitekey]',
]

# Button labels to click after filling an OTP (tried in order)
OTP_SUBMIT_TEXTS: list[str] = [
    "Verify", "Submit", "Continue", "Confirm", "Next", "OK", "Send",
]


# ── WhatsApp + DB coordination ────────────────────────────────────────────────

def request_otp_from_user(
    job_hash: str,
    company: str,
    platform_name: str,
    apply_url: str,
) -> None:
    """Send a WhatsApp OTP request and set ConversationState to ``pending_otp``."""
    from core.notifier import send_whatsapp
    from db.database import get_session
    from db.models import ConversationState

    logger.info(f"[{job_hash[:8]}] Requesting OTP from user for {platform_name}")

    msg = (
        f"🔐 *קוד אימות נדרש!*\n\n"
        f"🏢 *{company}* ({platform_name})\n\n"
        f"נא שלח את הקוד שקיבלת (OTP / verification code).\n"
        f"_יש לך {VERIFY_TIMEOUT_SECONDS // 60} דקות לענות._"
    )
    send_whatsapp(msg)

    session = get_session()
    try:
        row = session.query(ConversationState).first()
        if not row:
            row = ConversationState()
            session.add(row)
        row.state = "pending_otp"
        row.pending_job_hash = job_hash
        row.pending_field_label = platform_name
        row.field_answer = None
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    except Exception as exc:
        logger.warning(f"[{job_hash[:8]}] Failed to set pending_otp state: {exc}")
    finally:
        session.close()


def poll_for_otp(
    job_hash: str,
    timeout_seconds: int = VERIFY_TIMEOUT_SECONDS,
) -> str | None:
    """Block until the user supplies an OTP code via WhatsApp, or timeout.

    Returns the OTP string on success.  Returns ``None`` on timeout.
    The code value is NEVER written to any log.
    """
    from db.database import get_session
    from db.models import ConversationState

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        session = get_session()
        try:
            row = session.query(ConversationState).first()
            if (
                row
                and row.state == "otp_ready"
                and row.pending_job_hash == job_hash
                and row.field_answer
            ):
                code = row.field_answer
                # Erase from DB immediately — never hold plaintext longer than needed
                row.state = "idle"
                row.field_answer = None
                row.pending_job_hash = None
                row.updated_at = datetime.now(timezone.utc)
                session.commit()
                logger.info(f"[{job_hash[:8]}] OTP received from user [REDACTED]")
                return code
        except Exception as exc:
            logger.warning(f"[{job_hash[:8]}] OTP poll error: {exc}")
        finally:
            session.close()

        time.sleep(_POLL_INTERVAL)

    logger.warning(
        f"[{job_hash[:8]}] OTP poll timed out after {timeout_seconds}s — no user response"
    )
    return None


def request_human_intervention(
    job_hash: str,
    company: str,
    apply_url: str,
    reason: str,
) -> None:
    """Notify the user that manual action is needed and persist the waiting state.

    Sets ConversationState to ``pending_intervention`` so the webhook knows to
    resume the run when the user sends DONE.
    """
    from core.notifier import send_whatsapp
    from db.database import get_session
    from db.models import ConversationState

    logger.info(f"[{job_hash[:8]}] Human intervention required: {reason}")

    msg = (
        f"⚠️ *פעולה ידנית נדרשת!*\n\n"
        f"🏢 *{company}*\n"
        f"📋 {reason}\n\n"
        f"לאחר שסיימת, שלח *DONE* והמערכת תמשיך אוטומטית."
    )
    send_whatsapp(msg)

    session = get_session()
    try:
        row = session.query(ConversationState).first()
        if not row:
            row = ConversationState()
            session.add(row)
        row.state = "pending_intervention"
        row.pending_job_hash = job_hash
        row.pending_field_label = reason
        row.field_answer = None
        row.updated_at = datetime.now(timezone.utc)
        session.commit()
    except Exception as exc:
        logger.warning(f"[{job_hash[:8]}] Failed to set pending_intervention state: {exc}")
    finally:
        session.close()


def clear_verification_state() -> None:
    """Reset ConversationState to idle.  Call after OTP success or timeout cleanup."""
    from db.database import get_session
    from db.models import ConversationState

    session = get_session()
    try:
        row = session.query(ConversationState).first()
        if row and row.state in ("pending_otp", "otp_ready", "pending_intervention"):
            row.state = "idle"
            row.pending_job_hash = None
            row.pending_field_label = None
            row.field_answer = None
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
    except Exception as exc:
        logger.warning(f"Failed to clear verification state: {exc}")
    finally:
        session.close()
