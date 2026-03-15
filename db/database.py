from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from pathlib import Path

from db.models import Base

DB_PATH = Path(__file__).parent.parent / "data" / "jobtracker.db"


def get_engine():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{DB_PATH}", echo=False)


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    _migrate_legacy(engine)
    return engine


def _migrate_legacy(engine):
    """Add columns to legacy jobs table if they're missing."""
    legacy_columns = [
        ("source", "VARCHAR"),
        ("notes", "TEXT"),
        ("referral_type", "VARCHAR"),
        ("referral_url", "VARCHAR"),
        ("status_updated_at", "DATETIME"),
        ("application_method", "VARCHAR"),
        ("application_result", "VARCHAR"),
    ]
    with engine.connect() as conn:
        for col_name, col_type in legacy_columns:
            try:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}"))
                conn.commit()
            except Exception:
                pass  # Column already exists


def get_session():
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()


def is_duplicate(job_hash: str) -> bool:
    """Check if a job_hash exists in either suggested_jobs or applications."""
    from db.models import SuggestedJob, Application
    session = get_session()
    try:
        in_suggested = session.query(SuggestedJob).filter_by(job_hash=job_hash).first()
        if in_suggested:
            return True
        in_applied = session.query(Application).filter_by(job_hash=job_hash).first()
        return in_applied is not None
    finally:
        session.close()
