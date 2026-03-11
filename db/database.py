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
    _migrate_db(engine)
    return engine


def _migrate_db(engine):
    """Add new columns to existing tables without dropping data."""
    new_columns = [
        ("source", "VARCHAR"),
        ("notes", "TEXT"),
        ("referral_type", "VARCHAR"),
        ("referral_url", "VARCHAR"),
        ("status_updated_at", "DATETIME"),
    ]
    with engine.connect() as conn:
        for col_name, col_type in new_columns:
            try:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}"))
                conn.commit()
            except Exception:
                pass  # Column already exists


def get_session():
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()
