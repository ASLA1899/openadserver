"""
Ad-related database models.

Defines: Advertiser, Campaign, Creative, TargetingRule, HourlyStat
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from liteads.models.base import Base, BidType, CreativeType, EventType, Status, TimestampMixin


class Advertiser(Base, TimestampMixin):
    """Advertiser account."""

    __tablename__ = "advertisers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    daily_budget: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    status: Mapped[int] = mapped_column(Integer, default=Status.ACTIVE)

    # Relationships
    campaigns: Mapped[list["Campaign"]] = relationship(
        "Campaign", back_populates="advertiser", lazy="selectin"
    )


class Campaign(Base, TimestampMixin):
    """Advertising campaign."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    advertiser_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("advertisers.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Budget
    budget_daily: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    budget_total: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    spent_today: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    spent_total: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))

    # Bidding
    bid_type: Mapped[int] = mapped_column(Integer, default=BidType.CPM)
    bid_amount: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    priority_boost: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("1.00"))

    # Frequency cap
    freq_cap_daily: Mapped[int] = mapped_column(Integer, default=10)
    freq_cap_hourly: Mapped[int] = mapped_column(Integer, default=3)

    # Schedule
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Status
    status: Mapped[int] = mapped_column(Integer, default=Status.ACTIVE)

    # House ad flag (no cost, serves as fallback)
    is_house_ad: Mapped[bool] = mapped_column(Boolean, default=False)

    # Page targeting (JSON with include/exclude URL patterns)
    # Format: {"include": ["*/products/*", "*/blog/*"], "exclude": ["*/checkout/*"]}
    page_targeting: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Domain targeting (simple list of allowed domains)
    # Format: ["asla.org", "example.com"] - if set, only show on these domains
    target_domains: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # Stats (cached)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    advertiser: Mapped["Advertiser"] = relationship(
        "Advertiser", back_populates="campaigns"
    )
    creatives: Mapped[list["Creative"]] = relationship(
        "Creative", back_populates="campaign", lazy="selectin"
    )
    targeting_rules: Mapped[list["TargetingRule"]] = relationship(
        "TargetingRule", back_populates="campaign", lazy="selectin"
    )

    @property
    def is_active(self) -> bool:
        """Check if campaign is active based on status and schedule."""
        if self.status != Status.ACTIVE:
            return False

        from datetime import timezone
        now = datetime.now(timezone.utc)

        # Check start time - handle both naive and aware datetimes
        if self.start_time:
            start = self.start_time
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if now < start:
                return False

        # Check end time - handle both naive and aware datetimes
        if self.end_time:
            end = self.end_time
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if now > end:
                return False

        return True


class Creative(Base, TimestampMixin):
    """Ad creative (banner, video, native, etc.)."""

    __tablename__ = "creatives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    video_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    landing_url: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Creative metadata
    creative_type: Mapped[int] = mapped_column(Integer, default=CreativeType.BANNER)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)

    # Status
    status: Mapped[int] = mapped_column(Integer, default=Status.ACTIVE)

    # Quality score (0-100)
    quality_score: Mapped[int] = mapped_column(Integer, default=80)

    # Relationships
    campaign: Mapped["Campaign"] = relationship("Campaign", back_populates="creatives")


class TargetingRule(Base, TimestampMixin):
    """Targeting rules for campaigns."""

    __tablename__ = "targeting_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id"), nullable=False
    )

    # Rule type: geo, device, age, gender, interest, app_category, time, etc.
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Rule value (JSON) - flexible format for different rule types
    # Examples:
    # - geo: {"countries": ["CN", "US"], "cities": ["shanghai"]}
    # - device: {"os": ["android", "ios"], "brands": ["Samsung"]}
    # - age: {"min": 18, "max": 35}
    # - interest: {"categories": ["gaming", "sports"]}
    rule_value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Include (whitelist) or exclude (blacklist)
    is_include: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    campaign: Mapped["Campaign"] = relationship(
        "Campaign", back_populates="targeting_rules"
    )


class HourlyStat(Base):
    """Hourly statistics for campaigns."""

    __tablename__ = "hourly_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id"), nullable=False
    )
    stat_hour: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)
    spend: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))

    # Calculated metrics
    ctr: Mapped[Decimal] = mapped_column(Numeric(8, 6), default=Decimal("0"))
    cvr: Mapped[Decimal] = mapped_column(Numeric(8, 6), default=Decimal("0"))


class AdEvent(Base):
    """Ad event tracking (impressions, clicks, conversions)."""

    __tablename__ = "ad_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    creative_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[int] = mapped_column(Integer, nullable=False)  # EventType enum
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))

    # Optional metadata
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
