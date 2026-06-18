"""
database.py — SQLAlchemy engine + session management
Pattern: dependency injection via FastAPI's Depends()
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from backend.config import settings

engine = create_engine(
    settings.db_url(),
    pool_size=10,          # connection pool (important for FastAPI async loads)
    max_overflow=20,
    pool_pre_ping=True,    # drops stale connections automatically
    echo=(settings.app_env == "development"),  # SQL logging in dev only
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """
    FastAPI dependency — yields a DB session, always closes it.
    Usage: db: Session = Depends(get_db)
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()