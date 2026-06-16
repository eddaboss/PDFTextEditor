"""Account support tables, kept in their own module so the account system can
grow without reshaping the core ``models.py``.

  * ``EmailToken`` backs both the email-verification and password-reset links.
    Only a hash of each token is stored, so a database leak never yields a usable
    link; the raw token lives only in the email we send.
  * ``RateEvent`` is one row per sensitive request (login, register, reset). The
    middleware counts recent rows per client to throttle brute force, and prunes
    old ones as it goes so the table stays small.

``run_migrations()`` brings an already-deployed database up to the current shape
without Alembic: this project creates tables with ``Base.metadata.create_all``,
which never alters an existing table, so the one column we add to ``users`` is
backfilled here instead.
"""
import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, inspect, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# Token purposes (the ``purpose`` column on EmailToken).
PURPOSE_VERIFY = "verify"
PURPOSE_RESET = "reset"


def utcnow() -> datetime.datetime:
    """Naive UTC. These tables store naive timestamps so that the same Python
    comparison works whether the backend is Postgres (prod) or SQLite (dev/CI),
    which would otherwise hand back aware vs naive values and raise on compare."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class EmailToken(Base):
    __tablename__ = "email_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))
    # sha256 hex of the raw token. We look up by this; the raw value is emailed.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow)


class RateEvent(Base):
    __tablename__ = "rate_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "<action>:<client-key>", e.g. "login:198.51.100.4".
    bucket: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow)


# Composite index so the throttle's "rows in this bucket since T" query is cheap.
Index("ix_rate_events_bucket_time", RateEvent.bucket, RateEvent.created_at)


def run_migrations(engine) -> None:
    """Add columns this code expects but an older deployed ``users`` table lacks.
    Idempotent: each ALTER runs only when the column is missing, so it is safe on
    every boot and on a fresh database (where create_all already made them)."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    if "users" not in tables:
        return  # fresh DB; create_all builds users with the columns already

    cols = {c["name"] for c in insp.get_columns("users")}
    is_pg = engine.dialect.name == "postgresql"
    bool_false = "false" if is_pg else "0"
    ts_type = "TIMESTAMPTZ" if is_pg else "TIMESTAMP"

    stmts = []
    if "email_verified" not in cols:
        stmts.append(f"ALTER TABLE users ADD COLUMN email_verified BOOLEAN "
                     f"NOT NULL DEFAULT {bool_false}")
    if "email_verified_at" not in cols:
        stmts.append(f"ALTER TABLE users ADD COLUMN email_verified_at {ts_type}")

    if stmts:
        with engine.begin() as conn:
            for sql in stmts:
                conn.execute(text(sql))
