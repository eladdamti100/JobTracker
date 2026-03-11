"""Claude-powered job scoring against candidate profile."""

from loguru import logger


async def score_job(job: dict, profile: dict) -> dict:
    """Score a job listing against the candidate profile using Claude.

    Returns dict with keys:
        score, reason, role_type, is_student_position,
        tech_stack_match, apply_strategy
    """
    # TODO: Implement Claude API scoring
    logger.info(f"Scoring job: {job.get('title')} at {job.get('company')}")
    raise NotImplementedError("Analyzer not yet implemented")
