"""Auto form-filling and job application via Playwright + Claude Vision."""

from loguru import logger


async def apply_to_job(job_id: str) -> bool:
    """Automatically apply to a job by filling out the application form.

    Uses Playwright to navigate, Claude Vision to understand form fields,
    and fills based on candidate profile.

    Returns True on success, False on failure.
    """
    # TODO: Implement Phase 2 auto-apply
    logger.info(f"Applying to job: {job_id}")
    raise NotImplementedError("Applicator not yet implemented")
