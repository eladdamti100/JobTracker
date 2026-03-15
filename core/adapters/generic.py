"""Generic adapter — delegates to the existing apply_to_job() engine.

This adapter is the fallback for all platforms that do not have a
dedicated adapter yet.  It runs the full existing Playwright pipeline
and translates its dict result back into the orchestrator's StepResult
vocabulary.

Because apply_to_job() manages the entire browser lifecycle internally
(open → fill → submit → close), this adapter collapses all states into
a single fill_form() call.  Platform-specific adapters will split this
into finer-grained steps.
"""

from __future__ import annotations

from loguru import logger

from core.orchestrator import AdapterBase, ApplyState, StepResult, register_adapter


class GenericAdapter(AdapterBase):
    """Wraps the existing monolithic apply_to_job() function."""

    name = "generic"

    # ── plan ──────────────────────────────────────────────────────────────────

    def plan(self, checkpoint_meta: dict) -> StepResult:
        """For the generic adapter, skip straight to fill_form.

        The existing apply_to_job() handles login detection internally, so
        we don't need to split auth into separate states here.
        """
        logger.info(f"[{self.job_hash[:8]}] GenericAdapter.plan → FILL_FORM")
        return StepResult(ApplyState.FILL_FORM)

    # ── fill_form ─────────────────────────────────────────────────────────────

    def fill_form(self, checkpoint_meta: dict) -> StepResult:
        """Run the full apply pipeline via apply_to_job().

        Returns SUCCESS on success, FAILED on error.
        Screenshots and step counts are preserved in the result meta.
        """
        logger.info(
            f"[{self.job_hash[:8]}] GenericAdapter.fill_form — "
            f"launching apply_to_job for {self.apply_url}"
        )

        try:
            from core.applicator import apply_to_job

            raw: dict = apply_to_job(
                job_id=self.job_hash[:8],
                apply_url=self.apply_url,
                job_title=self.job_title,
                company=self.company,
                job_description=self.job_description,
                auto_submit=self.auto_submit,
                cv_variant=self.cv_variant,
            )
        except Exception as exc:
            logger.exception(
                f"[{self.job_hash[:8]}] apply_to_job raised: {exc}"
            )
            return StepResult(
                next_state=ApplyState.FAILED,
                success=False,
                error=str(exc),
            )

        success: bool = raw.get("success", False)
        error: str | None = raw.get("error")
        screenshot: str | None = raw.get("screenshot_path")

        # apply_to_job() returns steps_taken as an integer inside the dict
        meta = {
            "apply_to_job_steps": raw.get("steps_taken", 0),
            "raw_status": raw.get("status"),
        }

        if success:
            logger.info(
                f"[{self.job_hash[:8]}] GenericAdapter.fill_form → SUCCESS"
            )
            return StepResult(
                next_state=ApplyState.SUCCESS,
                success=True,
                screenshot_path=screenshot,
                meta=meta,
            )
        else:
            logger.warning(
                f"[{self.job_hash[:8]}] GenericAdapter.fill_form → FAILED: {error}"
            )
            return StepResult(
                next_state=ApplyState.FAILED,
                success=False,
                error=error,
                screenshot_path=screenshot,
                meta=meta,
            )

    # ── cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self, final_state: ApplyState, error: str | None) -> None:
        # apply_to_job() closes the browser internally; nothing to do here.
        logger.debug(
            f"[{self.job_hash[:8]}] GenericAdapter.cleanup "
            f"final_state={final_state.value}"
        )


# ── Self-registration ──────────────────────────────────────────────────────────
register_adapter("generic", GenericAdapter)
