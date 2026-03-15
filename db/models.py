import hashlib
from sqlalchemy import Column, String, Integer, Float, DateTime, Text, JSON
from sqlalchemy.orm import declarative_base
from datetime import datetime, timezone, timedelta


Base = declarative_base()


def make_job_hash(company: str, title: str, apply_url: str) -> str:
    """md5(company.lower() + title.lower() + apply_url) — unique job identity."""
    raw = f"{company.lower()}{title.lower()}{apply_url}"
    return hashlib.md5(raw.encode()).hexdigest()


class SuggestedJob(Base):
    __tablename__ = "suggested_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_hash = Column(String, unique=True, nullable=False, index=True)
    company = Column(String, nullable=False)
    title = Column(String, nullable=False)
    source = Column(String)               # HireMeTech / LinkedIn / WhatsApp
    apply_url = Column(String)
    location = Column(String)
    description = Column(Text)
    date_posted = Column(String)
    salary = Column(String)

    # Claude scoring
    score = Column(Float)
    reason = Column(Text)
    level = Column(String)                 # student / junior / senior
    role_type = Column(String)
    tech_stack_match = Column(JSON)
    is_student_position = Column(Integer)  # SQLite boolean
    apply_strategy = Column(String)
    role_summary = Column(Text)
    requirements_summary = Column(Text)

    # Lifecycle: suggested → approved → rejected → skipped → expired → applied
    status = Column(String, default="suggested", index=True)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, default=lambda: datetime.now(timezone.utc) + timedelta(hours=24))
    responded_at = Column(DateTime)

    def __repr__(self):
        return f"<SuggestedJob {self.job_hash[:8]}: {self.company} - {self.title} [{self.status}]>"


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_hash = Column(String, nullable=False, index=True)
    company = Column(String, nullable=False)
    title = Column(String, nullable=False)
    source = Column(String)
    apply_url = Column(String)

    # Application details
    applied_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    application_method = Column(String)    # "auto_apply" / "manual" / "easy_apply"
    application_result = Column(String)    # "success" / "failed" / "pending"
    status = Column(String, default="pending", index=True)  # success / failed / pending

    # Evidence
    screenshot_path = Column(String)
    cover_letter_used = Column(Text)
    error_message = Column(Text)

    def __repr__(self):
        return f"<Application {self.job_hash[:8]}: {self.company} - {self.title} [{self.status}]>"


class ConversationState(Base):
    """Single-row table tracking the current WhatsApp conversation state.

    States:
      idle              — no active conversation
      awaiting_feedback — user replied YES; waiting for instructions or confirmation
      pending_field     — applicator paused waiting for user answer to an unknown form field
    """
    __tablename__ = "conversation_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    state = Column(String, default="idle", nullable=False)
    pending_job_hash = Column(String)       # set when awaiting_feedback
    pending_field_label = Column(String)    # set when pending_field
    field_answer = Column(Text)             # filled by webhook when user answers a field
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ConversationState state={self.state!r} job={self.pending_job_hash}>"


# Keep legacy Job model so existing DB table isn't orphaned
class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, unique=True, nullable=False, index=True)
    title = Column(String, nullable=False)
    company = Column(String, nullable=False)
    location = Column(String)
    description = Column(Text)
    apply_url = Column(String)
    date_posted = Column(String)
    salary = Column(String)
    score = Column(Float)
    reason = Column(Text)
    level = Column(String)
    role_type = Column(String)
    tech_stack_match = Column(JSON)
    is_student_position = Column(Integer)
    apply_strategy = Column(String)
    role_summary = Column(Text)
    requirements_summary = Column(Text)
    status = Column(String, default="new", index=True)
    cover_letter_used = Column(Text)
    error_message = Column(Text)
    application_method = Column(String)
    application_result = Column(String)
    source = Column(String)
    notes = Column(Text)
    referral_type = Column(String)
    referral_url = Column(String)
    found_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    notified_at = Column(DateTime)
    applied_at = Column(DateTime)
    status_updated_at = Column(DateTime)

    def __repr__(self):
        return f"<Job {self.job_id}: {self.company} - {self.title} [{self.status}]>"
