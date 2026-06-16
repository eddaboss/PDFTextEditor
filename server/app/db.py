"""Database engine + session. Sync SQLAlchemy 2.0 over psycopg3.

FastAPI runs sync route handlers in a threadpool, so a plain sync engine is the
simplest correct choice for this small accounts API.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import DATABASE_URL


def _normalize(url: str) -> str:
    """Railway hands us ``postgresql://``; SQLAlchemy needs the psycopg3 driver
    prefix. Fall back to a local SQLite file so the app runs off-Railway too."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url or "sqlite+pysqlite:///./local-dev.db"


_url = _normalize(DATABASE_URL)
# The server runs sync handlers (and the throttle) across a threadpool, so the
# SQLite fallback needs one shared connection that any thread may use; without
# this SQLite refuses cross-thread reuse. These args are SQLite-only and do not
# affect the Postgres deploy.
_engine_kwargs = {"pool_pre_ping": True, "future": True}
if _url.startswith("sqlite"):
    _engine_kwargs.update(connect_args={"check_same_thread": False},
                          poolclass=StaticPool)

engine = create_engine(_url, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
