"""Event models for LiteAds."""
import enum
from datetime import datetime
from typing import Optional
from sqlalchemy import Column, Integer, String, DateTime, Enum as SQLEnum, ForeignKey
from liteads.models.base import Base, TimestampMixin


class EventType(enum.Enum):
    """Types of ad events."""
    IMPRESSION = "impression"
    CLICK = "click"
    CONVERSION = "conversion"


class AdEvent(Base, TimestampMixin):
    """Model for tracking ad events."""
    __tablename__ = "ad_events"

    id = Column(Integer, primary_key=True, index=True)
    creative_id = Column(Integer, ForeignKey("creatives.id"), nullable=False)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False)
    event_type = Column(SQLEnum(EventType), nullable=False)
    user_id = Column(String(255), nullable=True)
    session_id = Column(String(255), nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    referer = Column(String(500), nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
