from sqlalchemy import Column, String, Integer, Float, DateTime, Text, JSON
from sqlalchemy.orm import declarative_base
from datetime import datetime, timezone

Base = declarative_base()


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

    # Claude scoring
    score = Column(Float)
    reason = Column(Text)
    level = Column(String)            # student / junior / senior
    role_type = Column(String)
    tech_stack_match = Column(JSON)
    is_student_position = Column(Integer)  # SQLite boolean
    apply_strategy = Column(String)
    role_summary = Column(Text)       # Hebrew summary from Claude
    requirements_summary = Column(Text)  # Hebrew requirements from Claude

    # Lifecycle
    status = Column(String, default="new", index=True)
    # new → scored → notified → approved → applying → applied / failed
    cover_letter_used = Column(Text)
    error_message = Column(Text)

    # Dashboard / application tracking
    source = Column(String)               # HireMeTech / LinkedIn / WhatsApp
    notes = Column(Text)
    referral_type = Column(String)        # "referral" or "regular"
    referral_url = Column(String)

    # Timestamps
    found_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    notified_at = Column(DateTime)
    applied_at = Column(DateTime)
    status_updated_at = Column(DateTime)

    def __repr__(self):
        return f"<Job {self.job_id}: {self.company} - {self.title} [{self.status}]>"
