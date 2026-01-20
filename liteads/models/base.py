"""
Base model and common utilities for SQLAlchemy ORM.
"""

from datetime import datetime
from enum import IntEnum

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy declarative base class."""
    pass


class TimestampMixin:
    """Mixin for created_at and updated_at timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )


class Status(IntEnum):
    """Common status enum."""
    INACTIVE = 0
    ACTIVE = 1
    PAUSED = 2
    DELETED = 3
    PENDING = 4


class BidType(IntEnum):
    """Bid type enum."""
    CPM = 1        # Cost per mille (1000 impressions)
    CPC = 2        # Cost per click
    CPA = 3        # Cost per action/conversion
    OCPM = 4       # Optimized CPM
    FLAT_RATE = 5  # Prepaid flat rate (no per-event cost)


class CreativeType(IntEnum):
    """Creative type enum."""
    BANNER = 1
    NATIVE = 2
    VIDEO = 3
    INTERSTITIAL = 4


class EventType(IntEnum):
    """Event type enum."""
    IMPRESSION = 1
    CLICK = 2
    CONVERSION = 3
