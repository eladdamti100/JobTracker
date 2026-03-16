"""IMAP email listener for extracting verification links and codes.

Connects to Gmail via IMAP, searches for verification emails from job platforms,
extracts the verification URL or code, and handles it automatically.
"""

import os
import re
import imaplib
import email
import time
from email.header import decode_header
from datetime import datetime, timezone

import requests
from loguru import logger


# Known sender patterns for verification emails from job platforms
VERIFICATION_SENDERS = {
    "amazon": ["amazon.jobs", "amazon.com", "noreply@amazon"],
    "workday": ["workday.com", "myworkday"],
    "successfactors": ["successfactors", "sap.com"],
    "greenhouse": ["greenhouse.io"],
    "lever": ["lever.co"],
    "smartrecruiters": ["smartrecruiters.com"],
    "icims": ["icims.com"],
    "generic": [],  # fallback — search all recent emails
}

# Keywords that indicate a verification email
VERIFICATION_KEYWORDS = [
    "verify", "verification", "confirm your email", "activate your account",
    "confirm your account", "email confirmation", "validate", "complete registration",
    "verify your email", "confirm registration", "verification code",
]


def _connect() -> imaplib.IMAP4_SSL:
    """Connect to Gmail IMAP using App Password."""
    addr = os.environ.get("GMAIL_ADDRESS", "")
    pwd = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not addr or not pwd:
        raise ValueError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env")

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(addr, pwd)
    return mail


def _decode_subject(msg) -> str:
    """Decode email subject header."""
    raw = msg.get("Subject", "")
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _get_email_body(msg) -> str:
    """Extract text content from email (prefer HTML, fallback to plain)."""
    bodies = {"text/html": "", "text/plain": ""}
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in bodies:
                try:
                    bodies[ctype] = part.get_payload(decode=True).decode(errors="replace")
                except Exception:
                    pass
    else:
        ctype = msg.get_content_type()
        try:
            bodies[ctype] = msg.get_payload(decode=True).decode(errors="replace")
        except Exception:
            pass
    # Prefer HTML (usually has more content), strip tags for code extraction
    html = bodies["text/html"]
    if html:
        clean = re.sub(r'<[^>]+>', ' ', html)
        return re.sub(r'\s+', ' ', clean).strip()
    return bodies["text/plain"]


def _extract_links(msg) -> list[str]:
    """Extract all HTTP(S) links from an email message body.

    Extracts from both href attributes (HTML) and bare URLs (plain text).
    HTML entity decoding is applied so &amp; → & doesn't break URLs.
    """
    import html as _html

    def _from_body(body: str, is_html: bool) -> list[str]:
        found = []
        if is_html:
            # 1. Extract href/src attribute values first (most reliable for HTML emails)
            for attr_url in re.findall(r'href=["\']([^"\']+)["\']', body, re.I):
                decoded = _html.unescape(attr_url).strip()
                if decoded.startswith("http"):
                    found.append(decoded)
        # 2. Bare URL regex (catches plain-text and any href we missed)
        found.extend(re.findall(r'https?://[^\s<>"\']+', body))
        return found

    links = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                try:
                    body = part.get_payload(decode=True).decode(errors="replace")
                    links.extend(_from_body(body, is_html=(ctype == "text/html")))
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(errors="replace")
            ctype = msg.get_content_type()
            links.extend(_from_body(body, is_html=(ctype == "text/html")))
        except Exception:
            pass
    # Deduplicate while preserving order
    seen: set[str] = set()
    result = []
    for lnk in links:
        if lnk not in seen:
            seen.add(lnk)
            result.append(lnk)
    return result


def _extract_verification_code(body_text: str) -> str | None:
    """Extract a 4-8 digit verification code from email body text."""
    # Look for codes near verification keywords
    patterns = [
        r'(?:code|Code|CODE)[:\s]+(\d{4,8})',
        r'(\d{6})\s',  # standalone 6-digit code (most common)
        r'(?:enter|Enter|ENTER)[:\s]+(\d{4,8})',
    ]
    for pattern in patterns:
        m = re.search(pattern, body_text)
        if m:
            return m.group(1)

    # Fallback: find any 6-digit number that's likely a code
    codes = re.findall(r'\b(\d{6})\b', body_text)
    if len(codes) == 1:
        return codes[0]
    return None


def _is_verification_link(url: str) -> bool:
    """Check if a URL looks like a verification/confirmation link."""
    url_lower = url.lower()
    verify_keywords = [
        "verify", "confirm", "activate", "validate", "registration",
        "token=", "code=", "key=", "hash=", "auth",
    ]
    # Trusted domains that always serve verification links
    always_verify_domains = [
        "passport.services.amazon.jobs",
        "amazon.jobs/en/account/email_verify",
        "click.amazon-jobs",
    ]
    if any(d in url_lower for d in always_verify_domains):
        return True
    # Exclude by file extension or clear social/junk domains
    exclude_suffixes = [".png", ".jpg", ".jpeg", ".gif", ".css", ".svg", ".woff"]
    exclude_domains = [
        "facebook.com", "twitter.com", "linkedin.com/company",
        "instagram.com", "youtube.com",
    ]
    exclude_path_words = ["unsubscribe", "privacy", "terms", "/help", "/support",
                          "/logo", "/icon"]
    if any(url_lower.endswith(sfx) or ("?" not in url_lower and sfx in url_lower)
           for sfx in exclude_suffixes):
        return False
    if any(d in url_lower for d in exclude_domains):
        return False
    if any(w in url_lower for w in exclude_path_words):
        return False
    return any(kw in url_lower for kw in verify_keywords)


def find_verification_email(platform_key: str = "generic",
                            max_wait: int = 120,
                            poll_interval: int = 10) -> dict | None:
    """Search Gmail for a verification email from the given platform.

    Polls IMAP every `poll_interval` seconds for up to `max_wait` seconds.
    Returns dict with either {"type": "link", "value": "https://..."} or
    {"type": "code", "value": "123456"}, or None on timeout.
    """
    senders = VERIFICATION_SENDERS.get(platform_key, [])
    logger.info(f"Searching for verification email from {platform_key} "
                f"(senders: {senders}, timeout: {max_wait}s)")

    start = time.time()
    while time.time() - start < max_wait:
        try:
            mail = _connect()
            mail.select("INBOX")

            # Search last 2 days to handle UTC/local timezone edge cases
            from datetime import timedelta
            since_dt = datetime.now(timezone.utc) - timedelta(days=1)
            since_str = since_dt.strftime("%d-%b-%Y")
            _, msg_ids = mail.search(None, f'(SINCE "{since_str}")')
            ids = msg_ids[0].split()

            # Filter to emails received in the last 30 minutes by checking Date header
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)

            recent_ids = []
            for msg_id in ids:
                _, hdr = mail.fetch(msg_id, "(BODY[HEADER.FIELDS (DATE)])")
                raw_date = hdr[0][1].decode(errors="replace")
                date_str = re.sub(r"Date:\s*", "", raw_date, flags=re.I).strip()
                try:
                    from email.utils import parsedate_to_datetime
                    msg_dt = parsedate_to_datetime(date_str)
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                    if msg_dt >= cutoff:
                        recent_ids.append(msg_id)
                except Exception:
                    recent_ids.append(msg_id)  # include if we can't parse date

            for msg_id in reversed((recent_ids or ids)[-20:]):
                _, data = mail.fetch(msg_id, "(RFC822)")
                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                sender = (msg.get("From") or "").lower()
                subject = _decode_subject(msg).lower()

                sender_match = (not senders) or any(s in sender for s in senders)
                subject_match = any(kw in subject for kw in VERIFICATION_KEYWORDS)

                if sender_match and subject_match:
                    logger.info(f"Found verification email: {_decode_subject(msg)}")

                    # Mark as read so we don't pick it up again next call
                    mail.store(msg_id, '+FLAGS', '\\Seen')

                    # Try to extract a verification code first
                    body_text = _get_email_body(msg)
                    code = _extract_verification_code(body_text)
                    if code:
                        logger.info(f"Verification code found: {code}")
                        mail.logout()
                        return {"type": "code", "value": code}

                    # Try to extract a verification link
                    links = _extract_links(msg)
                    verify_links = [l for l in links if _is_verification_link(l)]
                    if verify_links:
                        link = verify_links[0].rstrip(">;)\"'")
                        logger.info(f"Verification link found: {link[:100]}...")
                        mail.logout()
                        return {"type": "link", "value": link}

                    # Fallback: longest link
                    long_links = [l for l in links if len(l) > 80]
                    if long_links:
                        link = long_links[0].rstrip(">;)\"'")
                        logger.info(f"Using longest link: {link[:100]}...")
                        mail.logout()
                        return {"type": "link", "value": link}

            mail.logout()

        except Exception as e:
            logger.warning(f"IMAP error: {e}")

        logger.debug(f"No verification email yet, retrying in {poll_interval}s...")
        time.sleep(poll_interval)

    logger.warning(f"Timeout waiting for verification email from {platform_key}")
    return None


def click_verification_link(url: str) -> bool:
    """Open a verification link via HTTP GET request."""
    try:
        logger.info(f"Clicking verification link: {url[:100]}...")
        resp = requests.get(url, timeout=30, allow_redirects=True)
        if resp.status_code < 400:
            logger.info(f"Verification link clicked (status={resp.status_code})")
            return True
        else:
            logger.warning(f"Verification link returned status {resp.status_code}")
            return False
    except Exception as e:
        logger.error(f"Failed to click verification link: {e}")
        return False


def auto_verify(platform_key: str = "generic",
                max_wait: int = 120) -> dict | None:
    """Full flow: find verification email → return result.

    Returns dict {"type": "link"|"code", "value": "..."} or None.
    For links, also clicks them automatically.
    """
    result = find_verification_email(platform_key, max_wait=max_wait)
    if not result:
        return None

    if result["type"] == "link":
        # Best-effort HTTP click — even if it fails, return the URL so the
        # caller can navigate the browser to it directly.
        click_verification_link(result["value"])

    return result
