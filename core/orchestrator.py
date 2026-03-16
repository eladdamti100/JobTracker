"""Apply Orchestrator — explicit state machine for job application runs.

Architecture
------------
ApplyOrchestrator owns the *what* (state transitions, retries, checkpointing).
Adapters own the *how* (browser interaction, platform-specific logic).

State machine
-------------
   DISCOVER ──► PLAN ──► RESTORE_SESSION ─┐
                   │                       ▼
                   ├──────────────────► LOGIN ──► VERIFY ─┐
                   │                       │               │
                   ├──────────────────► SIGNUP ──► VERIFY ─┤
                   │                                       │
                   └──────────────────► FILL_FORM ◄────────┘
                                            │
                                         REVIEW
                                            │
                                         SUBMIT
                                         /    \
                                      SUCCESS  FAILED

Any state can transition to:
  HUMAN_INTERVENTION  — CAPTCHA / MFA / manual step required
  FAILED              — retry limit exceeded or unrecoverable error

Retry / backoff
---------------
  attempt_count is persisted in ApplyCheckpoint.
  Backoff formula: min(2**attempt * 3, 60) seconds — caps at 60 s.
  Auth states (LOGIN/SIGNUP) have a separate lower cap to prevent lockouts.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger


# ── State enum ────────────────────────────────────────────────────────────────

class ApplyState(str, Enum):
    """All possible states in an application run."""
    DISCOVER            = "discover"            # resolve URL, detect platform
    PLAN                = "plan"                # choose adapter, decide auth path
    RESTORE_SESSION     = "restore_session"     # try to reuse saved cookies
    LOGIN               = "login"               # log in with stored credentials
    SIGNUP              = "signup"              # create new account
    VERIFY              = "verify"              # email/OTP verification
    FILL_FORM           = "fill_form"           # fill application fields
    REVIEW              = "review"              # pre-submit review page
    SUBMIT              = "submit"              # click submit / final confirmation
    HUMAN_INTERVENTION  = "human_intervention"  # CAPTCHA / MFA — pause for user
    SUCCESS             = "success"             # application submitted
    FAILED              = "failed"              # unrecoverable failure


# Terminal states — the machine stops here.
TERMINAL_STATES = {ApplyState.SUCCESS, ApplyState.FAILED, ApplyState.HUMAN_INTERVENTION}

# States that risk account lockouts — capped at MAX_AUTH_ATTEMPTS independently.
AUTH_STATES = {ApplyState.LOGIN, ApplyState.SIGNUP}

# Default retry limits (overridden by config/settings.py values)
MAX_TOTAL_ATTEMPTS   = 5
MAX_AUTH_ATTEMPTS    = 2
MAX_BACKOFF_SECONDS  = 60

# Stale checkpoint thresholds (hours without progress before resetting to PLAN)
_STALE_HOURS_DEFAULT           = 2    # active flow states
_STALE_HOURS_HUMAN_INTERVENTION = 24  # user may need time to act


# ── Step result — returned by every adapter method ────────────────────────────

class StepResult:
    """Structured outcome returned by an adapter step.

    Attributes
    ----------
    next_state : ApplyState
        Where the orchestrator should go next.
    success : bool
        Whether this step completed its goal.
    error : str | None
        Human-readable error message if success is False.
    screenshot_path : str | None
        Path to a screenshot taken during this step.
    meta : dict
        Arbitrary adapter-specific data persisted in ApplyCheckpoint.metadata_json.
    """

    def __init__(
        self,
        next_state: ApplyState,
        success: bool = True,
        error: str | None = None,
        screenshot_path: str | None = None,
        meta: dict | None = None,
    ):
        self.next_state = next_state
        self.success = success
        self.error = error
        self.screenshot_path = screenshot_path
        self.meta = meta or {}

    def __repr__(self) -> str:
        return (
            f"<StepResult next={self.next_state.value} "
            f"ok={self.success} err={self.error!r}>"
        )


# ── Apply result — returned to the caller ─────────────────────────────────────

class ApplyResult:
    """Final outcome of an application run."""

    def __init__(
        self,
        success: bool,
        job_hash: str,
        final_state: ApplyState,
        error: str | None = None,
        screenshot_path: str | None = None,
        steps_taken: int = 0,
        adapter_name: str = "unknown",
    ):
        self.success = success
        self.job_hash = job_hash
        self.final_state = final_state
        self.error = error
        self.screenshot_path = screenshot_path
        self.steps_taken = steps_taken
        self.adapter_name = adapter_name

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "job_hash": self.job_hash,
            "final_state": self.final_state.value,
            "error": self.error,
            "screenshot_path": self.screenshot_path,
            "steps_taken": self.steps_taken,
            "adapter_name": self.adapter_name,
        }

    def __repr__(self) -> str:
        return (
            f"<ApplyResult job={self.job_hash[:8]} "
            f"state={self.final_state.value} ok={self.success}>"
        )


# ── Base adapter interface ─────────────────────────────────────────────────────

class AdapterBase:
    """Abstract base for all platform adapters.

    Subclasses override the step methods they care about.
    The default implementation for every step returns FAILED so that a missing
    override is caught immediately rather than silently skipped.

    Lifecycle
    ---------
    The orchestrator calls methods in this order (skipping steps that are not
    applicable based on the previous StepResult):

        plan()           → decides auth path, sets adapter_name
        restore_session() → tries cached cookies
        login()          → fills email/password form
        signup()         → creates a new account
        verify()         → handles OTP / email verification
        fill_form()      → fills the application fields
        review()         → handles any review/confirm page
        submit()         → clicks the final submit button
        cleanup()        → always called — close browser, save state, etc.
    """

    #: Canonical name — used in checkpoints and logs.
    name: str = "base"

    def __init__(self, job_hash: str, apply_url: str, job_title: str,
                 company: str, job_description: str, auto_submit: bool = False,
                 cv_variant: str | None = None):
        self.job_hash = job_hash
        self.apply_url = apply_url
        self.job_title = job_title
        self.company = company
        self.job_description = job_description
        self.auto_submit = auto_submit
        self.cv_variant = cv_variant

        # Mutable context shared across step calls
        self.ctx: dict[str, Any] = {}

    # ── Step methods ─────────────────────────────────────────────────────────

    def plan(self, checkpoint_meta: dict) -> StepResult:
        """Inspect the landing page and decide the initial auth path.

        Returns a StepResult whose next_state is one of:
          RESTORE_SESSION, LOGIN, SIGNUP, FILL_FORM.
        """
        return StepResult(ApplyState.FAILED, success=False,
                          error=f"{self.name}.plan() not implemented")

    def restore_session(self, checkpoint_meta: dict) -> StepResult:
        """Attempt to load saved cookies and verify the session is still valid.

        Returns FILL_FORM if the session is live, LOGIN otherwise.
        """
        return StepResult(ApplyState.LOGIN)

    def login(self, checkpoint_meta: dict) -> StepResult:
        """Fill and submit the login form.

        Returns FILL_FORM on success, VERIFY if MFA/OTP is needed,
        SIGNUP if no account exists, FAILED on unrecoverable error.
        """
        return StepResult(ApplyState.FAILED, success=False,
                          error=f"{self.name}.login() not implemented")

    def signup(self, checkpoint_meta: dict) -> StepResult:
        """Create a new account on the platform.

        Returns VERIFY if email confirmation is required, FILL_FORM on
        immediate success, FAILED otherwise.
        """
        return StepResult(ApplyState.FAILED, success=False,
                          error=f"{self.name}.signup() not implemented")

    def verify(self, checkpoint_meta: dict) -> StepResult:
        """Handle email link / OTP / MFA verification.

        Returns FILL_FORM on success, HUMAN_INTERVENTION if manual action
        is needed (e.g. CAPTCHA), FAILED on timeout.
        """
        return StepResult(ApplyState.FAILED, success=False,
                          error=f"{self.name}.verify() not implemented")

    def fill_form(self, checkpoint_meta: dict) -> StepResult:
        """Fill all application form fields.

        Returns REVIEW if there is a review page, SUBMIT otherwise.
        """
        return StepResult(ApplyState.FAILED, success=False,
                          error=f"{self.name}.fill_form() not implemented")

    def review(self, checkpoint_meta: dict) -> StepResult:
        """Handle the review/confirm page if present.

        Returns SUBMIT.
        """
        return StepResult(ApplyState.SUBMIT)

    def submit(self, checkpoint_meta: dict) -> StepResult:
        """Click the final submit button and confirm the application was sent.

        Returns SUCCESS or FAILED.
        """
        return StepResult(ApplyState.FAILED, success=False,
                          error=f"{self.name}.submit() not implemented")

    def cleanup(self, final_state: ApplyState, error: str | None) -> None:
        """Always called at the end of a run — close browser, save session, etc."""


# ── Orchestrator ──────────────────────────────────────────────────────────────

class ApplyOrchestrator:
    """Runs the application state machine for a single job.

    Usage
    -----
    >>> result = ApplyOrchestrator(job, auto_submit=True).run()
    >>> print(result.success, result.final_state)

    Or for resuming a crashed run (checkpoint is loaded automatically):
    >>> result = ApplyOrchestrator(job, auto_submit=True).run()
    """

    def __init__(
        self,
        job,                       # db.models.SuggestedJob instance
        auto_submit: bool = False,
        max_total_attempts: int | None = None,
        max_auth_attempts: int | None = None,
    ):
        self.job = job
        self.auto_submit = auto_submit

        from config.settings import MAX_LOGIN_ATTEMPTS
        self.max_total_attempts = max_total_attempts or MAX_TOTAL_ATTEMPTS
        self.max_auth_attempts = max_auth_attempts or MAX_LOGIN_ATTEMPTS

        self._adapter: AdapterBase | None = None
        self._checkpoint = None         # db.models.ApplyCheckpoint row
        self._auth_attempt_count = 0    # separate counter for LOGIN/SIGNUP
        self._steps_taken = 0

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> ApplyResult:
        """Run (or resume) the application state machine.

        Returns an ApplyResult regardless of outcome — never raises.
        """
        try:
            return self._run_internal()
        except Exception as exc:
            logger.exception(f"Orchestrator crashed for {self.job.job_hash[:8]}: {exc}")
            self._save_checkpoint(
                ApplyState.FAILED,
                error=str(exc),
                screenshot_path=None,
            )
            return ApplyResult(
                success=False,
                job_hash=self.job.job_hash,
                final_state=ApplyState.FAILED,
                error=f"Orchestrator crash: {exc}",
                steps_taken=self._steps_taken,
                adapter_name=self._adapter.name if self._adapter else "unknown",
            )

    # ── Internal state machine ────────────────────────────────────────────────

    def _run_internal(self) -> ApplyResult:
        self._checkpoint = self._load_or_create_checkpoint()
        state = ApplyState(self._checkpoint.current_state)
        meta: dict = self._checkpoint.metadata_json or {}

        # ── Browser-less resume recovery ──────────────────────────────────────
        # Every new process starts with no browser.  If the checkpoint has a
        # mid-flow state (anything other than PLAN/DISCOVER/terminal/HUMAN_INTERVENTION),
        # we MUST reset to PLAN so the adapter re-opens the browser and re-detects
        # the actual page state.
        #
        # HUMAN_INTERVENTION is the only mid-flow state we deliberately keep —
        # the user is expected to act and then send DONE, which resets the
        # checkpoint to PLAN explicitly (see webhook._handle_done).
        is_browser_less_resume = (
            state not in TERMINAL_STATES
            and state not in {ApplyState.PLAN, ApplyState.DISCOVER,
                              ApplyState.HUMAN_INTERVENTION}
        )
        if is_browser_less_resume:
            logger.warning(
                f"[{self.job.job_hash[:8]}] New process, no browser — "
                f"resetting from {state.value} → PLAN to re-open browser"
            )
            state = ApplyState.PLAN
            self._checkpoint.attempt_count = 0

        logger.info(
            f"[{self.job.job_hash[:8]}] Orchestrator starting from state={state.value} "
            f"attempt={self._checkpoint.attempt_count}"
        )

        # Select adapter (done once; re-used if resuming)
        self._adapter = _select_adapter(
            job_hash=self.job.job_hash,
            apply_url=self.job.apply_url,
            job_title=self.job.title,
            company=self.job.company,
            job_description=self.job.description or "",
            auto_submit=self.auto_submit,
            cv_variant=getattr(self.job, "cv_variant", None),
        )
        self._save_checkpoint(state, meta=meta)

        error: str | None = None
        screenshot_path: str | None = None

        while state not in TERMINAL_STATES:
            self._steps_taken += 1

            # Guard: total attempt limit
            if self._checkpoint.attempt_count >= self.max_total_attempts:
                logger.warning(
                    f"[{self.job.job_hash[:8]}] Total attempt limit "
                    f"({self.max_total_attempts}) reached — stopping."
                )
                state = ApplyState.FAILED
                error = f"Exceeded {self.max_total_attempts} total attempts"
                break

            # Guard: auth attempt limit
            if state in AUTH_STATES and self._auth_attempt_count >= self.max_auth_attempts:
                logger.warning(
                    f"[{self.job.job_hash[:8]}] Auth attempt limit "
                    f"({self.max_auth_attempts}) reached — stopping to avoid lockout."
                )
                state = ApplyState.FAILED
                error = f"Exceeded {self.max_auth_attempts} auth attempts (lockout guard)"
                break

            logger.info(f"[{self.job.job_hash[:8]}] ── State: {state.value} ──")

            step_result = self._dispatch(state, meta)
            screenshot_path = step_result.screenshot_path or screenshot_path
            meta.update(step_result.meta)

            if not step_result.success:
                error = step_result.error
                # Retry with backoff unless the adapter already set a terminal state
                if step_result.next_state not in TERMINAL_STATES:
                    self._checkpoint.attempt_count += 1
                    backoff = _backoff_seconds(self._checkpoint.attempt_count)
                    logger.warning(
                        f"[{self.job.job_hash[:8]}] Step failed: {error} — "
                        f"retrying in {backoff}s (attempt {self._checkpoint.attempt_count})"
                    )
                    self._save_checkpoint(state, error=error, meta=meta,
                                          screenshot_path=screenshot_path)
                    time.sleep(backoff)
                    # Stay in the same state to retry
                    continue

            if state in AUTH_STATES:
                self._auth_attempt_count += 1

            prev_state = state
            state = step_result.next_state
            logger.info(
                f"[{self.job.job_hash[:8]}] {prev_state.value} → {state.value}"
            )
            self._save_checkpoint(state, error=step_result.error, meta=meta,
                                  screenshot_path=screenshot_path)

        # ── Teardown ──────────────────────────────────────────────────────────
        if self._adapter:
            try:
                self._adapter.cleanup(state, error)
            except Exception as exc:
                logger.warning(f"Adapter cleanup error: {exc}")

        # Don't mark the job failed while waiting for human action — the
        # checkpoint persists the state and the webhook DONE command resumes it.
        if state == ApplyState.HUMAN_INTERVENTION:
            logger.info(
                f"[{self.job.job_hash[:8]}] Paused at HUMAN_INTERVENTION — "
                "job status unchanged (remains 'approved')"
            )
            result = ApplyResult(
                success=False,
                job_hash=self.job.job_hash,
                final_state=state,
                error=error,
                screenshot_path=screenshot_path,
                steps_taken=self._steps_taken,
                adapter_name=self._adapter.name if self._adapter else "unknown",
            )
            logger.info(f"[{self.job.job_hash[:8]}] Run complete: {result}")
            return result

        success = (state == ApplyState.SUCCESS)
        self._update_job_status(success, error)

        result = ApplyResult(
            success=success,
            job_hash=self.job.job_hash,
            final_state=state,
            error=error,
            screenshot_path=screenshot_path,
            steps_taken=self._steps_taken,
            adapter_name=self._adapter.name if self._adapter else "unknown",
        )
        logger.info(f"[{self.job.job_hash[:8]}] Run complete: {result}")
        return result

    def _dispatch(self, state: ApplyState, meta: dict) -> StepResult:
        """Call the correct adapter method for *state*."""
        dispatch: dict[ApplyState, Any] = {
            ApplyState.DISCOVER:         self._adapter.plan,      # DISCOVER reuses plan()
            ApplyState.PLAN:             self._adapter.plan,
            ApplyState.RESTORE_SESSION:  self._adapter.restore_session,
            ApplyState.LOGIN:            self._adapter.login,
            ApplyState.SIGNUP:           self._adapter.signup,
            ApplyState.VERIFY:           self._adapter.verify,
            ApplyState.FILL_FORM:        self._adapter.fill_form,
            ApplyState.REVIEW:           self._adapter.review,
            ApplyState.SUBMIT:           self._adapter.submit,
        }
        fn = dispatch.get(state)
        if fn is None:
            return StepResult(
                ApplyState.FAILED, success=False,
                error=f"No handler for state {state.value}",
            )
        try:
            return fn(meta)
        except Exception as exc:
            logger.exception(
                f"[{self.job.job_hash[:8]}] Adapter raised in {state.value}: {exc}"
            )
            return StepResult(
                ApplyState.FAILED, success=False, error=str(exc)
            )

    # ── Checkpoint helpers ────────────────────────────────────────────────────

    def _load_or_create_checkpoint(self):
        from db.database import get_session
        from db.models import ApplyCheckpoint

        session = get_session()
        try:
            cp = (
                session.query(ApplyCheckpoint)
                .filter_by(suggested_job_id=self.job.job_hash)
                .first()
            )

            if cp:
                # ── Terminal states: always start fresh ───────────────────────
                is_terminal = cp.current_state in (
                    ApplyState.SUCCESS.value, ApplyState.FAILED.value,
                )
                # ── Stale detection ───────────────────────────────────────────
                is_stale = _is_stale_checkpoint(cp)
                if is_stale:
                    logger.warning(
                        f"[{self.job.job_hash[:8]}] Checkpoint is stale "
                        f"(state={cp.current_state}, last_update={cp.updated_at}) "
                        f"— resetting to PLAN"
                    )

                if not is_terminal and not is_stale:
                    logger.info(
                        f"[{self.job.job_hash[:8]}] Resuming from checkpoint "
                        f"state={cp.current_state} attempt={cp.attempt_count}"
                    )
                    return cp

                # Reset to PLAN (terminal, stale, or inconsistent)
                cp.current_state = ApplyState.PLAN.value
                cp.attempt_count = 0
                cp.last_error = None
                cp.metadata_json = {}
                cp.updated_at = datetime.now(timezone.utc)
            else:
                cp = ApplyCheckpoint(
                    suggested_job_id=self.job.job_hash,
                    current_state=ApplyState.PLAN.value,
                    adapter_name="unknown",
                    attempt_count=0,
                )
                session.add(cp)

            session.commit()
            session.refresh(cp)
            return cp
        finally:
            session.close()

    def _save_checkpoint(
        self,
        state: ApplyState,
        error: str | None = None,
        screenshot_path: str | None = None,
        meta: dict | None = None,
    ) -> None:
        from db.database import get_session
        from core.log_utils import redact_secrets

        session = get_session()
        try:
            from db.models import ApplyCheckpoint
            cp = (
                session.query(ApplyCheckpoint)
                .filter_by(suggested_job_id=self.job.job_hash)
                .first()
            )
            if not cp:
                return
            cp.current_state = state.value
            cp.adapter_name = self._adapter.name if self._adapter else "unknown"
            # Redact secrets from error messages before persisting
            cp.last_error = redact_secrets(error) if error else None
            if screenshot_path:
                cp.last_screenshot_path = screenshot_path
            if meta is not None:
                existing = cp.metadata_json or {}
                existing.update(meta)
                cp.metadata_json = existing
            cp.updated_at = datetime.now(timezone.utc)
            session.commit()
        except Exception as exc:
            logger.warning(f"Failed to save checkpoint: {exc}")
        finally:
            session.close()

    # ── Job status update ─────────────────────────────────────────────────────

    def _update_job_status(self, success: bool, error: str | None) -> None:
        """Update SuggestedJob status and create/update Application record."""
        from db.database import get_session
        from db.models import SuggestedJob, Application

        session = get_session()
        try:
            job = session.query(SuggestedJob).filter_by(
                job_hash=self.job.job_hash
            ).first()
            if job:
                job.status = "applied" if success else "failed"

            app = session.query(Application).filter_by(
                job_hash=self.job.job_hash
            ).first()
            result_str = "success" if success else "failed"
            if app:
                app.application_result = result_str
                app.status = result_str
                if error:
                    app.error_message = error
            else:
                app = Application(
                    job_hash=self.job.job_hash,
                    company=self.job.company,
                    title=self.job.title,
                    source=getattr(self.job, "source", None),
                    apply_url=self.job.apply_url,
                    application_method="auto_apply",
                    application_result=result_str,
                    status=result_str,
                    error_message=error,
                )
                session.add(app)
            session.commit()
        except Exception as exc:
            logger.warning(f"Failed to update job status: {exc}")
            session.rollback()
        finally:
            session.close()


# ── Adapter registry ──────────────────────────────────────────────────────────
#
# Maps platform keys to adapter classes.
# Populated lazily when each adapter module is imported.
# Add new adapters here or call register_adapter() from the adapter module.

_ADAPTER_REGISTRY: dict[str, type[AdapterBase]] = {}


def register_adapter(platform_key: str, adapter_cls: type[AdapterBase]) -> None:
    """Register a platform adapter.  Called by each adapter module at import time."""
    _ADAPTER_REGISTRY[platform_key] = adapter_cls
    logger.debug(f"Registered adapter: {platform_key} → {adapter_cls.__name__}")


def _select_adapter(
    job_hash: str,
    apply_url: str,
    job_title: str,
    company: str,
    job_description: str,
    auto_submit: bool,
    cv_variant: str | None,
) -> AdapterBase:
    """Resolve the best adapter for *apply_url*.

    Priority:
    1. Exact platform key match in _ADAPTER_REGISTRY
    2. Falls back to GenericAdapter (wraps the existing apply_to_job engine)
    """
    from core.credential_manager import resolve_platform_key

    # Import the adapters package so all platform adapters register themselves
    # before we look them up in _ADAPTER_REGISTRY.  Safe to call multiple times
    # (Python caches imported modules).
    import core.adapters  # noqa: F401

    platform_key = resolve_platform_key(apply_url)
    # strip "generic:" prefix for registry look-up
    bare_key = platform_key.removeprefix("generic:")

    adapter_cls = (
        _ADAPTER_REGISTRY.get(platform_key)
        or _ADAPTER_REGISTRY.get(bare_key)
        or _ADAPTER_REGISTRY.get("generic")
    )

    if adapter_cls is None:
        # Import lazily to avoid circular deps; GenericAdapter always registers itself
        from core.adapters.generic_adapter import GenericAdapter  # noqa: F401
        adapter_cls = _ADAPTER_REGISTRY.get("generic", GenericAdapter)

    logger.info(
        f"[{job_hash[:8]}] Platform={platform_key} → adapter={adapter_cls.__name__}"
    )

    return adapter_cls(
        job_hash=job_hash,
        apply_url=apply_url,
        job_title=job_title,
        company=company,
        job_description=job_description,
        auto_submit=auto_submit,
        cv_variant=cv_variant,
    )


# ── Convenience function ──────────────────────────────────────────────────────

def run_application(job, auto_submit: bool = False) -> ApplyResult:
    """Top-level entry point used by the webhook and CLI.

    Replaces the direct call to ``apply_to_job()`` everywhere in the codebase.

    >>> result = run_application(job, auto_submit=True)
    >>> if result.success:
    ...     notify_user(f"Applied to {job.company}!")
    """
    return ApplyOrchestrator(job, auto_submit=auto_submit).run()


# ── Backoff ───────────────────────────────────────────────────────────────────

def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff capped at MAX_BACKOFF_SECONDS."""
    return min(2 ** attempt * 3, MAX_BACKOFF_SECONDS)


# ── Stale checkpoint detection ─────────────────────────────────────────────────

def _is_stale_checkpoint(cp) -> bool:
    """Return True if *cp* has not progressed in too long.

    Thresholds
    ----------
    HUMAN_INTERVENTION  24 h  — user may need time to respond
    all other states     2 h  — a normal apply run takes < 30 min
    """
    if cp.updated_at is None:
        return False
    now = datetime.now(timezone.utc)
    # Make updated_at timezone-aware if it isn't (SQLite stores naive UTC)
    updated = cp.updated_at
    if updated.tzinfo is None:
        from datetime import timezone as _tz
        updated = updated.replace(tzinfo=_tz.utc)
    age_hours = (now - updated).total_seconds() / 3600

    threshold = (
        _STALE_HOURS_HUMAN_INTERVENTION
        if cp.current_state == ApplyState.HUMAN_INTERVENTION.value
        else _STALE_HOURS_DEFAULT
    )
    return age_hours > threshold
