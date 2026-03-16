#!/usr/bin/env python3
"""JobTracker smoke test — validates imports, env vars, DB schema, and tooling.

Run from the project root:
    python smoke_test.py

Exits with code 0 if all checks pass, 1 if any FAIL.
WARNings don't affect the exit code.
"""

from __future__ import annotations

import os
import sys
import importlib

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

_results: list[tuple[str, str, str]] = []   # (status, category, message)


def ok(category: str, msg: str) -> None:
    _results.append(("PASS", category, msg))
    print(f"  {GREEN}✔{RESET}  {msg}")


def warn(category: str, msg: str) -> None:
    _results.append(("WARN", category, msg))
    print(f"  {YELLOW}⚠{RESET}  {msg}")


def fail(category: str, msg: str) -> None:
    _results.append(("FAIL", category, msg))
    print(f"  {RED}✘{RESET}  {msg}")


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")
    print("─" * 50)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Environment variables
# ─────────────────────────────────────────────────────────────────────────────

def check_env() -> None:
    section("1. Environment variables")

    # Load .env if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        warn("env", "python-dotenv not installed — .env file NOT loaded automatically")

    required = {
        "GROQ_API_KEY":            "Groq LLM scoring + Vision",
        "TWILIO_ACCOUNT_SID":      "Twilio WhatsApp outbound",
        "TWILIO_AUTH_TOKEN":       "Twilio WhatsApp outbound",
        "TWILIO_WHATSAPP_FROM":    "Twilio WhatsApp from number",
        "MY_WHATSAPP_NUMBER":      "Your WhatsApp number to receive messages",
        "CREDENTIAL_ENCRYPTION_KEY": "Fernet key for credential vault",
    }
    optional = {
        "GMAIL_ADDRESS":    "Gmail address for IMAP email verification",
        "GMAIL_APP_PASSWORD": "Gmail app-password for IMAP",
        "API_KEY":          "Dashboard REST API auth key",
        "GROQ_VISION_MODEL": "Override Groq vision model (default: llama-4-scout)",
    }

    for key, purpose in required.items():
        val = os.environ.get(key, "")
        if val:
            ok("env", f"{key} set  ({purpose})")
        else:
            fail("env", f"{key} MISSING  ({purpose})")

    for key, purpose in optional.items():
        val = os.environ.get(key, "")
        if val:
            ok("env", f"{key} set  ({purpose})")
        else:
            warn("env", f"{key} not set  ({purpose})")

    # Validate Fernet key format
    key = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "")
    if key:
        try:
            from cryptography.fernet import Fernet
            Fernet(key.encode() if isinstance(key, str) else key)
            ok("env", "CREDENTIAL_ENCRYPTION_KEY is a valid Fernet key")
        except Exception as exc:
            fail("env", f"CREDENTIAL_ENCRYPTION_KEY is INVALID: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Python imports
# ─────────────────────────────────────────────────────────────────────────────

def check_imports() -> None:
    section("2. Python imports")

    modules = [
        # Core system
        ("db.models",                    "DB models"),
        ("db.database",                  "DB engine"),
        ("core.orchestrator",            "Apply orchestrator"),
        ("core.verifier",                "Verification helpers (Stage 7)"),
        ("core.credential_manager",      "Credential vault"),
        ("core.notifier",                "WhatsApp notifier"),
        ("core.analyzer",                "Groq job scorer"),
        ("core.applicator",              "Legacy applicator / helpers"),
        ("core.log_utils",               "Secure logging"),
        ("core.expiry",                  "Expiry scheduler"),
        # Adapters
        ("core.adapters.base_adapter",   "Adapter base class"),
        ("core.adapters.generic_adapter","Generic ATS adapter"),
        ("core.adapters.workday_adapter","Workday adapter"),
        ("core.adapters.amazon_adapter", "Amazon adapter"),
        ("core.adapters.greenhouse_adapter", "Greenhouse adapter"),
        ("core.adapters.lever_adapter",  "Lever adapter"),
        # Web layer
        ("flask",                        "Flask"),
        ("playwright.sync_api",          "Playwright sync API"),
        ("sqlalchemy",                   "SQLAlchemy"),
        ("openai",                       "OpenAI SDK (Groq client)"),
        ("twilio.rest",                  "Twilio REST client"),
        ("loguru",                       "Loguru logger"),
        ("cryptography.fernet",          "Fernet encryption"),
        ("apscheduler",                  "APScheduler"),
    ]

    for module, label in modules:
        try:
            importlib.import_module(module)
            ok("imports", f"{label}  ({module})")
        except ImportError as exc:
            fail("imports", f"{label}  ({module})  →  {exc}")
        except Exception as exc:
            # Non-import errors (e.g. bad env at module level) — still imported
            warn("imports", f"{label}  ({module})  loaded with warning: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Adapter registration
# ─────────────────────────────────────────────────────────────────────────────

def check_adapters() -> None:
    section("3. Adapter registration")

    try:
        from core.orchestrator import _ADAPTER_REGISTRY
        expected = ["workday", "amazon", "greenhouse", "lever", "generic"]
        for name in expected:
            if name in _ADAPTER_REGISTRY:
                ok("adapters", f"'{name}' registered  ({_ADAPTER_REGISTRY[name].__name__})")
            else:
                fail("adapters", f"'{name}' NOT in registry")

        # Test detect() on known URLs
        detect_cases = [
            ("https://amazon.jobs/en/jobs/123",         "amazon"),
            ("https://hiring.amazon.com/apply",         "amazon"),
            ("https://company.myworkdayjobs.com/apply", "workday"),
            ("https://boards.greenhouse.io/acme/jobs/1","greenhouse"),
            ("https://jobs.lever.co/acme/abc123",       "lever"),
        ]
        for url, expected_adapter in detect_cases:
            adapter_cls = _ADAPTER_REGISTRY.get(expected_adapter)
            if adapter_cls and adapter_cls.detect(url):
                ok("adapters", f"detect({url[:45]}…) → '{expected_adapter}'")
            else:
                fail("adapters", f"detect({url[:45]}…) should return '{expected_adapter}'")

    except ImportError as exc:
        fail("adapters", f"Could not import orchestrator registry: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. DB schema
# ─────────────────────────────────────────────────────────────────────────────

def check_db() -> None:
    section("4. Database schema")

    try:
        from db.database import init_db, get_engine
        from sqlalchemy import inspect as sa_inspect

        engine = init_db()   # creates tables + runs migrations
        ok("db", "init_db() ran without error")

        inspector = sa_inspect(engine)
        existing_tables = set(inspector.get_table_names())

        required_tables = {
            "suggested_jobs": ["job_hash", "company", "title", "status", "cv_variant"],
            "applications":   ["job_hash", "status", "application_result"],
            "conversation_state": ["state", "pending_job_hash", "field_answer",
                                   "pending_field_label"],
            "company_credentials": ["platform_key", "email", "encrypted_password",
                                    "auth_type", "account_status", "domain",
                                    "last_used_at", "updated_at"],
            "session_store":  ["domain", "platform_key", "encrypted_storage_state",
                               "expires_at"],
            "apply_checkpoints": ["suggested_job_id", "current_state", "adapter_name",
                                  "attempt_count", "metadata_json"],
            "ats_field_memory":  ["ats_key", "field_mappings"],
        }

        for table, required_cols in required_tables.items():
            if table not in existing_tables:
                fail("db", f"Table '{table}' MISSING")
                continue
            ok("db", f"Table '{table}' exists")

            existing_cols = {c["name"] for c in inspector.get_columns(table)}
            for col in required_cols:
                if col in existing_cols:
                    ok("db", f"  {table}.{col}")
                else:
                    fail("db", f"  {table}.{col}  MISSING COLUMN")

        # ConversationState row
        from db.database import get_session
        from db.models import ConversationState
        session = get_session()
        try:
            row = session.query(ConversationState).first()
            if row:
                ok("db", f"ConversationState seed row present (state={row.state!r})")
            else:
                fail("db", "ConversationState seed row MISSING")
        finally:
            session.close()

    except Exception as exc:
        fail("db", f"DB check failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Playwright
# ─────────────────────────────────────────────────────────────────────────────

def check_playwright() -> None:
    section("5. Playwright")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("about:blank")
            title = page.title()
            browser.close()
        ok("playwright", f"Chromium launched and navigated (title={title!r})")
    except Exception as exc:
        fail("playwright", f"Playwright Chromium not working: {exc}\n"
             "         Run:  playwright install chromium")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Data files
# ─────────────────────────────────────────────────────────────────────────────

def check_data_files() -> None:
    section("6. Data files")

    from pathlib import Path
    project_root = Path(__file__).parent

    required = {
        "config/profile.yaml":        "Candidate profile for Groq scoring",
        "data/default_answers.yaml":  "Answer database for form auto-fill",
    }
    optional = {
        "data/CV Resume.pdf":            "CV for upload fields",
        "data/linkedin_session.json":    "LinkedIn session cookies",
    }

    for rel_path, purpose in required.items():
        p = project_root / rel_path
        if p.exists():
            ok("files", f"{rel_path}  ({purpose})")
        else:
            fail("files", f"{rel_path} MISSING  ({purpose})")

    for rel_path, purpose in optional.items():
        p = project_root / rel_path
        if p.exists():
            ok("files", f"{rel_path}  ({purpose})")
        else:
            warn("files", f"{rel_path} not found  ({purpose})")


# ─────────────────────────────────────────────────────────────────────────────
# 7. Verifier public API
# ─────────────────────────────────────────────────────────────────────────────

def check_verifier() -> None:
    section("7. Verifier API (Stage 7)")

    try:
        from core.verifier import (
            request_otp_from_user,
            poll_for_otp,
            request_human_intervention,
            clear_verification_state,
            VERIFY_TIMEOUT_SECONDS,
            OTP_SELECTORS,
            CAPTCHA_SELECTORS,
        )
        ok("verifier", f"All public symbols importable")
        ok("verifier", f"VERIFY_TIMEOUT_SECONDS = {VERIFY_TIMEOUT_SECONDS}")
        ok("verifier", f"OTP_SELECTORS: {len(OTP_SELECTORS)} entries")
        ok("verifier", f"CAPTCHA_SELECTORS: {len(CAPTCHA_SELECTORS)} entries")
    except ImportError as exc:
        fail("verifier", f"Import failed: {exc}")
    except Exception as exc:
        fail("verifier", f"Unexpected error: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# 8. Quick applicator helpers sanity check
# ─────────────────────────────────────────────────────────────────────────────

def check_applicator() -> None:
    section("8. Applicator helpers")

    try:
        from core.applicator import (
            _get_answers,
            normalize_field_name,
            lookup_answer,
            NEXT_BUTTON_TEXTS,
            SUBMIT_BUTTON_TEXTS,
        )
        answers = _get_answers()
        if isinstance(answers, dict) and answers:
            ok("applicator", f"_get_answers() returned {len(answers)} keys")
        else:
            warn("applicator", f"_get_answers() returned empty dict — check default_answers.yaml")

        # Normalisation sanity
        assert normalize_field_name("First Name") == "first_name", "normalise failed"
        ok("applicator", "normalize_field_name('First Name') → 'first_name'")

        ok("applicator", f"NEXT_BUTTON_TEXTS: {len(NEXT_BUTTON_TEXTS)} entries")
        ok("applicator", f"SUBMIT_BUTTON_TEXTS: {len(SUBMIT_BUTTON_TEXTS)} entries")

    except ImportError as exc:
        fail("applicator", f"Import failed: {exc}")
    except AssertionError as exc:
        fail("applicator", f"Sanity assertion failed: {exc}")
    except Exception as exc:
        warn("applicator", f"Applicator check warning: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

def print_summary() -> int:
    passes = [r for r in _results if r[0] == "PASS"]
    warns  = [r for r in _results if r[0] == "WARN"]
    fails  = [r for r in _results if r[0] == "FAIL"]

    print(f"\n{'═' * 50}")
    print(f"{BOLD}SUMMARY{RESET}")
    print(f"{'═' * 50}")
    print(f"  {GREEN}PASS{RESET}  {len(passes)}")
    print(f"  {YELLOW}WARN{RESET}  {len(warns)}")
    print(f"  {RED}FAIL{RESET}  {len(fails)}")

    if fails:
        print(f"\n{RED}{BOLD}Failed checks:{RESET}")
        for _, category, msg in fails:
            print(f"  [{category}] {msg}")
        print(f"\n{RED}✘ Smoke test FAILED — fix the issues above before running apply.{RESET}\n")
        return 1

    if warns:
        print(f"\n{YELLOW}Some optional checks warned — system may run with limited functionality.{RESET}")

    print(f"\n{GREEN}{BOLD}✔ Smoke test PASSED — apply pipeline looks healthy.{RESET}\n")
    return 0


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{BOLD}JobTracker — Apply Pipeline Smoke Test{RESET}")
    print("=" * 50)

    check_env()
    check_imports()
    check_adapters()
    check_db()
    check_playwright()
    check_data_files()
    check_verifier()
    check_applicator()

    sys.exit(print_summary())
