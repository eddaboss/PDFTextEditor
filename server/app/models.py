"""Persistent models: the user account and a record of terms agreements. The
door is open for future opt-in features (cloud storage, etc.) without reshaping
this."""
import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    # Whether the email address has been confirmed via a verification link. It
    # gates nothing in the app today; it is the foundation for any future opt-in
    # feature that needs a confirmed address. New rows added to an existing table
    # are backfilled by account_models.run_migrations().
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false")
    email_verified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class Consent(Base):
    """A click-through record that someone agreed to the Terms + Privacy Policy
    before downloading. Captured at the download gate, keyed by the email the
    user gives, then linked to their account (``account_id``) once they register
    with that email. Stores the agreed terms version plus request metadata so
    the agreement is provable."""

    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    terms_version: Mapped[str] = mapped_column(String(32))
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(Text, default="")
    # True once the person proved the address via the emailed 6-digit code (new
    # visitors) or authenticated with an account on that email. Added to the
    # existing table by account_models.run_migrations().
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
