"""Secure credential vault for job-application platforms.

Public API
----------
encrypt_secret(value)          -> str          # Fernet-encrypt any string
decrypt_secret(ciphertext)     -> str          # Fernet-decrypt
generate_secure_password(n)    -> str          # Cryptographically random password

get_credential(platform_key)   -> (email, password) | None
save_credential(platform_key, email, password, ...)
mark_login_success(platform_key)

resolve_platform_key(url)      -> str          # URL → platform key

Security rules enforced here
-----------------------------
- Secrets are NEVER written to logs (only redacted forms are logged).
- The encryption key is loaded exclusively from CREDENTIAL_ENCRYPTION_KEY env var.
- Never implement custom cryptography — only cryptography.fernet.Fernet is used.
"""

from __future__ import annotations

import os
import secrets
import string
from datetime import datetime, timezone
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger


# ── Platform detection ─────────────────────────────────────────────────────────
# URL hostname substring → canonical platform key
PLATFORM_PATTERNS: dict[str, str] = {
    "myworkdayjobs.com": "workday",
    "myworkdaysite.com": "workday",
    "myworkday.com": "workday",
    "amazon.jobs": "amazon",
    "successfactors": "successfactors",
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq.com": "ashby",
    "breezyhr.com": "breezyhr",
    "sparkhire.com": "sparkhire",
    "smartrecruiters.com": "smartrecruiters",
    "icims.com": "icims",
    "taleo": "taleo",
    "applytojob.com": "applytojob",
    "jobvite.com": "jobvite",
    "bamboohr.com": "bamboohr",
    "recruitee.com": "recruitee",
}

# Platforms where auto-signup is explicitly disabled — require manual credential entry
PLATFORMS_NO_AUTO_SIGNUP: set[str] = set()

# ── CSS selectors per platform ────────────────────────────────────────────────
PLATFORM_SELECTORS: dict[str, dict[str, str]] = {
    "workday": {
        "email": 'input[data-automation-id="email"], input[type="email"]',
        "password": 'input[data-automation-id="password"], input[type="password"]',
        "signin": 'button[data-automation-id="signInSubmitButton"]',
        "create_account": 'a[data-automation-id="createAccountLink"]',
    },
    "successfactors": {
        "email": 'input[id*="username"], input[type="email"]',
        "password": 'input[id*="password"], input[type="password"]',
    },
}

# Common login button labels (tried in order)
LOGIN_BUTTON_TEXTS: list[str] = [
    "Sign In", "Log In", "Login", "Sign in", "Log in",
    "Submit", "Continue", "Next",
]

# Common signup / create-account link labels (tried in order)
SIGNUP_LINK_TEXTS: list[str] = [
    "Sign Up", "Create Account", "Register", "Create an Account",
    "Create an Amazon.jobs account", "New User", "Sign up", "Create account",
    "Don't have an account", "Not a registered user", "Create one",
]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get_fernet() -> Fernet:
    """Load Fernet cipher from CREDENTIAL_ENCRYPTION_KEY env var.

    Raises ValueError if the key is missing or invalid — never falls back to a
    default to avoid silently weakening security.
    """
    key = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not key:
        raise ValueError(
            "CREDENTIAL_ENCRYPTION_KEY is not set. "
            "Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise ValueError(f"CREDENTIAL_ENCRYPTION_KEY is invalid: {exc}") from exc


# ── Public cryptography API ────────────────────────────────────────────────────

def encrypt_secret(value: str) -> str:
    """Fernet-encrypt *value*. Returns a base64-encoded ciphertext string.

    Never logs the plaintext value.
    """
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Fernet-decrypt *ciphertext*. Returns plaintext.

    Raises InvalidToken if the ciphertext is tampered or the key is wrong.
    Never logs the plaintext result.
    """
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise InvalidToken(
            "Decryption failed — ciphertext may be corrupted or the "
            "CREDENTIAL_ENCRYPTION_KEY has changed."
        ) from exc


# Backward-compatible aliases used in older call sites
encrypt_password = encrypt_secret
decrypt_password = decrypt_secret


def generate_secure_password(length: int = 24) -> str:
    """Generate a cryptographically random password.

    Guarantees at least one uppercase letter, one lowercase letter, one digit,
    and one special character, to satisfy most platform complexity requirements.
    Never logs the generated password.
    """
    if length < 8:
        raise ValueError("Password length must be at least 8.")
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.isupper() for c in password)
            and any(c.islower() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in "!@#$%^&*" for c in password)
        ):
            return password


# ── Platform key resolution ────────────────────────────────────────────────────

def resolve_platform_key(url: str) -> str:
    """Map a job-application URL to a canonical platform key.

    Falls back to ``"generic:<hostname>"`` for unknown sites.
    """
    hostname = (urlparse(url).hostname or "").lower()
    for pattern, key in PLATFORM_PATTERNS.items():
        if pattern in hostname:
            return key
    return f"generic:{hostname}"


# ── DB-backed credential CRUD ──────────────────────────────────────────────────

def get_credential(platform_key: str) -> tuple[str, str] | None:
    """Return ``(email, plaintext_password)`` for *platform_key*, or ``None``.

    Never logs the password.
    """
    from db.database import get_session
    from db.models import CompanyCredential

    session = get_session()
    try:
        cred = (
            session.query(CompanyCredential)
            .filter_by(platform_key=platform_key)
            .first()
        )
        if not cred:
            return None
        password = decrypt_secret(cred.encrypted_password)
        logger.info(f"Found stored credentials for {platform_key} ({cred.email})")
        return (cred.email, password)
    except Exception as exc:
        logger.error(f"Failed to retrieve credentials for {platform_key}: {exc}")
        return None
    finally:
        session.close()


def save_credential(
    platform_key: str,
    email: str,
    password: str,
    domain: str = "",
    auth_type: str = "password",
    notes: str = "",
) -> None:
    """Encrypt and upsert credentials for *platform_key*.

    Never logs the password.
    """
    from db.database import get_session
    from db.models import CompanyCredential

    encrypted = encrypt_secret(password)
    session = get_session()
    try:
        cred = (
            session.query(CompanyCredential)
            .filter_by(platform_key=platform_key)
            .first()
        )
        now = datetime.now(timezone.utc)
        if cred:
            cred.email = email
            cred.encrypted_password = encrypted
            cred.domain = domain or cred.domain
            cred.auth_type = auth_type
            cred.notes = notes
            cred.updated_at = now
            logger.info(f"Updated credentials for {platform_key} ({email})")
        else:
            cred = CompanyCredential(
                platform_key=platform_key,
                email=email,
                encrypted_password=encrypted,
                domain=domain,
                auth_type=auth_type,
                account_status="active",
                notes=notes,
            )
            session.add(cred)
            logger.info(f"Saved new credentials for {platform_key} ({email})")
        session.commit()
    except Exception as exc:
        logger.error(f"Failed to save credentials for {platform_key}: {exc}")
        session.rollback()
    finally:
        session.close()


def mark_login_success(platform_key: str) -> None:
    """Increment login_success_count and update last_used_at for *platform_key*."""
    from db.database import get_session
    from db.models import CompanyCredential

    session = get_session()
    try:
        cred = (
            session.query(CompanyCredential)
            .filter_by(platform_key=platform_key)
            .first()
        )
        if cred:
            now = datetime.now(timezone.utc)
            cred.last_used_at = now
            cred.last_used = now          # backward-compat alias column
            cred.login_success_count = (cred.login_success_count or 0) + 1
            cred.account_status = "active"
            session.commit()
    finally:
        session.close()


def mark_account_status(platform_key: str, status: str) -> None:
    """Set account_status for *platform_key*.

    Valid values: ``active``, ``pending_verification``, ``needs_reauth``, ``blocked``.
    """
    from db.database import get_session
    from db.models import CompanyCredential

    session = get_session()
    try:
        cred = (
            session.query(CompanyCredential)
            .filter_by(platform_key=platform_key)
            .first()
        )
        if cred:
            cred.account_status = status
            cred.updated_at = datetime.now(timezone.utc)
            session.commit()
            logger.info(f"Account status for {platform_key} → {status}")
    finally:
        session.close()


# ── Session store helpers ──────────────────────────────────────────────────────

def save_session_state(
    domain: str,
    platform_key: str,
    storage_state: str,
    expires_at: datetime | None = None,
) -> None:
    """Encrypt and persist a Playwright storage_state JSON blob.

    Never logs the raw storage state (it contains session tokens).
    """
    from db.database import get_session
    from db.models import SessionStore

    encrypted = encrypt_secret(storage_state)
    session = get_session()
    try:
        row = (
            session.query(SessionStore)
            .filter_by(domain=domain)
            .first()
        )
        now = datetime.now(timezone.utc)
        if row:
            row.encrypted_storage_state = encrypted
            row.platform_key = platform_key
            row.expires_at = expires_at
            row.updated_at = now
            row.last_used_at = now
        else:
            row = SessionStore(
                domain=domain,
                platform_key=platform_key,
                encrypted_storage_state=encrypted,
                expires_at=expires_at,
            )
            session.add(row)
        session.commit()
        logger.info(f"Session state saved for {domain} [REDACTED]")
    except Exception as exc:
        logger.error(f"Failed to save session state for {domain}: {exc}")
        session.rollback()
    finally:
        session.close()


def load_session_state(domain: str) -> str | None:
    """Load and decrypt a Playwright storage_state JSON blob, or return None.

    Never logs the decrypted value.
    """
    from db.database import get_session
    from db.models import SessionStore

    session = get_session()
    try:
        row = (
            session.query(SessionStore)
            .filter_by(domain=domain)
            .first()
        )
        if not row:
            return None
        now = datetime.now(timezone.utc)
        if row.expires_at and row.expires_at < now:
            logger.info(f"Session state for {domain} has expired — ignoring")
            return None
        row.last_used_at = now
        session.commit()
        logger.info(f"Loaded session state for {domain} [REDACTED]")
        return decrypt_secret(row.encrypted_storage_state)
    except Exception as exc:
        logger.error(f"Failed to load session state for {domain}: {exc}")
        return None
    finally:
        session.close()
