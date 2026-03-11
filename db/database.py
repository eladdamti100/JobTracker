from sqlalchemy import create_engine
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
    return engine


def get_session():
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()
