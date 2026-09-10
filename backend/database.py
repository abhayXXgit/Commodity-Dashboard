"""SQLAlchemy engine / session factory. Works on SQLite and PostgreSQL."""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from backend.config import settings

_connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_con, _rec):            # pragma: no cover
        cur = dbapi_con.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    """Transactional scope for jobs and scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def db_since(hours: float) -> "dt.datetime":
    """
    A timestamp `hours` ago, in the flavour this database actually stores.

    SQLite has no timezone type: SQLAlchemy writes a naive string, so comparing
    it against a tz-aware value silently matches nothing. PostgreSQL uses
    timestamptz and needs the aware value. Getting this wrong makes every
    "have we already done this?" check answer "no", which is how a daily job
    ends up duplicating its output every single run.
    """
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=hours)
    if engine.dialect.name == "sqlite":
        return cutoff.replace(tzinfo=None)
    return cutoff


def as_aware_utc(value: "dt.datetime | None") -> "dt.datetime | None":
    """Read back a stored timestamp as tz-aware UTC regardless of dialect."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def init_db() -> None:
    from backend import models  # noqa: F401  (registers mappers)
    Base.metadata.create_all(bind=engine)
