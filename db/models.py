import hashlib
from sqlalchemy import Column, String, Integer, Float, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
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

    # CV variant (e.g. "CV-Backend", "CV-DevOps") — None means default CV
    cv_variant = Column(String, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, default=lambda: datetime.now(timezone.utc) + timedelta(hours=24))
    responded_at = Column(DateTime)

    # Relationship
    applications = relationship("Application", back_populates="suggested_job")

    def __repr__(self):
        return f"<SuggestedJob {self.job_hash[:8]}: {self.company} - {self.title} [{self.status}]>"


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_hash = Column(String, ForeignKey("suggested_jobs.job_hash"), nullable=False, index=True)
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

    # Relationship
    suggested_job = relationship("SuggestedJob", back_populates="applications")

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


class ATSFieldMemory(Base):
    """Cache discovered form field mappings per ATS platform.

    After a successful application, the CSS selectors and field mappings
    are saved here. On subsequent applications to the same ATS, cached
    mappings are tried first — skipping the expensive Claude Vision call.
    """
    __tablename__ = "ats_field_memory"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ats_key = Column(String, unique=True, index=True)  # e.g. "comeet", "greenhouse"
    field_mappings = Column(JSON)   # {canonical_field: {label, type, selector}}
    success_count = Column(Integer, default=1)
    last_used = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ATSFieldMemory {self.ats_key} (success_count={self.success_count})>"
