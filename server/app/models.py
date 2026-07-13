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
    # The anonymous analytics cookie of the browser that agreed, so the metrics
    # dashboard can stitch the funnel visit -> download -> account. Added to the
    # existing table by account_models.run_migrations().
    visitor_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class Event(Base):
    """One analytics event -- a page view, a download, or a desktop update
    check-in. Anonymous by design: keyed by a random ``visitor_id`` cookie, never
    an email. Powers the private metrics dashboard (visits vs downloads, geo,
    the visit->download->signup funnel, active installs). New table, so
    ``Base.metadata.create_all`` builds it; no migration needed."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
    # "pageview" (a site visit), "download" (an installer fetch), or "ping" (the
    # desktop updater checking in -- a live-install signal).
    kind: Mapped[str] = mapped_column(String(16), index=True)
    visitor_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    path: Mapped[str] = mapped_column(String(255), default="")
    referrer: Mapped[str] = mapped_column(String(512), default="")
    # From Cloudflare's visitor-location headers (needs the "Add visitor location
    # headers" managed transform on). country is ISO-2.
    country: Mapped[str] = mapped_column(String(2), default="", index=True)
    region: Mapped[str] = mapped_column(String(80), default="")
    city: Mapped[str] = mapped_column(String(128), default="")
    platform: Mapped[str] = mapped_column(String(16), default="")  # mac|windows|other
    app_version: Mapped[str] = mapped_column(String(32), default="")
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
