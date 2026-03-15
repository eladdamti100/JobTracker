"""Encrypted credential storage for job application platforms.

Stores and retrieves login credentials (email + Fernet-encrypted password)
per platform so the applicator can log in or sign up automatically.
"""

import os
import secrets
import string
from datetime import datetime, timezone
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from loguru import logger


# Platform detection: URL hostname substring → platform key
PLATFORM_PATTERNS = {
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
}

# Platforms where we should NOT auto-create accounts (user must provide credentials)
PLATFORMS_NO_AUTO_SIGNUP = set()

# Platform-specific CSS selectors for login forms
PLATFORM_SELECTORS = {
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

# Common login button texts
LOGIN_BUTTON_TEXTS = [
    "Sign In", "Log In", "Login", "Sign in", "Log in",
    "Submit", "Continue", "Next",
]

# Common signup link texts
SIGNUP_LINK_TEXTS = [
    "Sign Up", "Create Account", "Register", "Create an Account",
    "Create an Amazon.jobs account", "New User", "Sign up", "Create account",
    "Don't have an account", "Not a registered user", "Create one",
]


def _get_fernet() -> Fernet:
    """Load Fernet cipher from CREDENTIAL_ENCRYPTION_KEY env var."""
    key = os.environ.get("CREDENTIAL_ENCRYPTION_KEY")
    if not key:
        raise ValueError(
            "CREDENTIAL_ENCRYPTION_KEY not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def encrypt_password(plaintext: str) -> str:
    """Encrypt a password. Returns base64-encoded ciphertext string."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext: str) -> str:
    """Decrypt a stored password. Returns plaintext."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


def generate_secure_password(length: int = 16) -> str:
    """Generate a random password meeting common complexity requirements."""
    # Ensure at least one of each: uppercase, lowercase, digit, special
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        password = ''.join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.isupper() for c in password) and
            any(c.islower() for c in password) and
            any(c.isdigit() for c in password) and
            any(c in "!@#$%^&*" for c in password)):
            return password


def resolve_platform_key(url: str) -> str:
    """Determine the platform key from a URL.

    Checks against PLATFORM_PATTERNS, falls back to 'generic:<hostname>'.
    """
    hostname = urlparse(url).hostname or ""
    for pattern, key in PLATFORM_PATTERNS.items():
        if pattern in hostname:
            return key
    return f"generic:{hostname}"


def get_credential(platform_key: str) -> tuple[str, str] | None:
    """Look up stored credentials. Returns (email, password) or None."""
    from db.database import get_session
    from db.models import CompanyCredential

    session = get_session()
    try:
        cred = session.query(CompanyCredential).filter_by(
            platform_key=platform_key
        ).first()
        if not cred:
            return None
        password = decrypt_password(cred.encrypted_password)
        logger.info(f"Found stored credentials for {platform_key} ({cred.email})")
        return (cred.email, password)
    except Exception as e:
        logger.error(f"Failed to retrieve credentials for {platform_key}: {e}")
        return None
    finally:
        session.close()


def save_credential(platform_key: str, email: str, password: str,
                    notes: str = "") -> None:
    """Encrypt and store credentials. Upserts by platform_key."""
    from db.database import get_session
    from db.models import CompanyCredential

    encrypted = encrypt_password(password)
    session = get_session()
    try:
        cred = session.query(CompanyCredential).filter_by(
            platform_key=platform_key
        ).first()
        if cred:
            cred.email = email
            cred.encrypted_password = encrypted
            cred.notes = notes
            logger.info(f"Updated credentials for {platform_key}")
        else:
            cred = CompanyCredential(
                platform_key=platform_key,
                email=email,
                encrypted_password=encrypted,
                notes=notes,
            )
            session.add(cred)
            logger.info(f"Saved new credentials for {platform_key} ({email})")
        session.commit()
    except Exception as e:
        logger.error(f"Failed to save credentials for {platform_key}: {e}")
        session.rollback()
    finally:
        session.close()


def mark_login_success(platform_key: str) -> None:
    """Update last_used and increment login_success_count."""
    from db.database import get_session
    from db.models import CompanyCredential

    session = get_session()
    try:
        cred = session.query(CompanyCredential).filter_by(
            platform_key=platform_key
        ).first()
        if cred:
            cred.last_used = datetime.now(timezone.utc)
            cred.login_success_count = (cred.login_success_count or 0) + 1
            session.commit()
    finally:
        session.close()
