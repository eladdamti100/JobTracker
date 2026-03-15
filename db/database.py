from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
from pathlib import Path
from loguru import logger

from db.models import Base

DB_PATH = Path(__file__).parent.parent / "data" / "jobtracker.db"


def get_engine():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{DB_PATH}", echo=False)


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    _migrate(engine)
    return engine


def _migrate(engine):
    """Run all schema migrations."""
    _add_column_if_missing(engine, "suggested_jobs", "cv_variant", "VARCHAR")
    _migrate_legacy_jobs(engine)


def _add_column_if_missing(engine, table: str, column: str, col_type: str):
    """Safely add a column to an existing table."""
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return
    existing = [c["name"] for c in inspector.get_columns(table)]
    if column not in existing:
        with engine.connect() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
            conn.commit()
            logger.info(f"Migration: added {table}.{column}")


def _migrate_legacy_jobs(engine):
    """Migrate any remaining legacy Job records into SuggestedJob, then drop jobs table."""
    inspector = inspect(engine)
    if "jobs" not in inspector.get_table_names():
        return

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM jobs")).fetchall()
        if rows:
            from db.models import SuggestedJob, make_job_hash
            Session = sessionmaker(bind=engine)
            session = Session()
            migrated = 0
            for row in rows:
                row_dict = row._mapping
                job_hash = make_job_hash(
                    row_dict.get("company", ""),
                    row_dict.get("title", ""),
                    row_dict.get("apply_url", ""),
                )
                exists = session.query(SuggestedJob).filter_by(job_hash=job_hash).first()
                if not exists:
                    sj = SuggestedJob(
                        job_hash=job_hash,
                        company=row_dict.get("company", ""),
                        title=row_dict.get("title", ""),
                        source=row_dict.get("source"),
                        apply_url=row_dict.get("apply_url"),
                        location=row_dict.get("location"),
                        description=row_dict.get("description"),
                        date_posted=row_dict.get("date_posted"),
                        salary=row_dict.get("salary"),
                        score=row_dict.get("score"),
                        reason=row_dict.get("reason"),
                        level=row_dict.get("level"),
                        role_type=row_dict.get("role_type"),
                        tech_stack_match=row_dict.get("tech_stack_match"),
                        is_student_position=row_dict.get("is_student_position"),
                        apply_strategy=row_dict.get("apply_strategy"),
                        role_summary=row_dict.get("role_summary"),
                        requirements_summary=row_dict.get("requirements_summary"),
                        status=row_dict.get("status", "suggested"),
                    )
                    session.add(sj)
                    migrated += 1
            session.commit()
            session.close()
            if migrated:
                logger.info(f"Migration: moved {migrated} legacy jobs to suggested_jobs")

        conn.execute(text("DROP TABLE jobs"))
        conn.commit()
        logger.info("Migration: dropped legacy jobs table")


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
