"""Persistent models: the user account and a record of terms agreements. The
door is open for future opt-in features (cloud storage, etc.) without reshaping
this."""
import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(120), default="")
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
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
