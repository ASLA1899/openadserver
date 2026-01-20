"""
Event tracking service.

Handles recording ad events (impressions, clicks, conversions).
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from liteads.common.cache import CacheKeys, redis_client
from liteads.common.logger import get_logger
from liteads.common.utils import current_date, current_hour
from liteads.models import AdEvent, EventType
from liteads.models.ad import Campaign
from liteads.models.base import BidType

logger = get_logger(__name__)


class EventService:
    """Event tracking service."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def track_event(
        self,
        request_id: str,
        ad_id: str,
        event_type: str,
        user_id: str | None = None,
        timestamp: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """
        Track an ad event.

        Events are:
        1. Persisted to database for billing/reporting
        2. Cached in Redis for real-time stats
        3. Used for frequency control updates
        """
        try:
            # Parse ad ID to get campaign/creative IDs
            campaign_id, creative_id = self._parse_ad_id(ad_id)

            # Convert event type
            event_type_enum = self._get_event_type(event_type)
            if event_type_enum is None:
                logger.warning(f"Unknown event type: {event_type}")
                return False

            # Create event record
            event = AdEvent(
                request_id=request_id,
                campaign_id=campaign_id,
                creative_id=creative_id,
                event_type=event_type_enum,
                event_time=datetime.fromtimestamp(timestamp, tz=timezone.utc)
                if timestamp
                else datetime.now(timezone.utc),
                user_id=user_id,
                cost=await self._calculate_cost(event_type_enum, campaign_id),
            )

            self.session.add(event)
            await self.session.flush()

            # Update real-time stats in Redis
            await self._update_stats(campaign_id, event_type_enum)

            # Update frequency counter for impressions
            if event_type_enum == EventType.IMPRESSION and user_id:
                await self._update_frequency(user_id, campaign_id)

            logger.info(
                "Event tracked",
                event_id=event.id,
                campaign_id=campaign_id,
            )

            return True

        except Exception as e:
            logger.error(f"Failed to track event: {e}")
            return False

    def _parse_ad_id(self, ad_id: str) -> tuple[int | None, int | None]:
        """Parse ad ID to extract campaign and creative IDs."""
        # Supports both formats:
        # - New: {campaign_id}_{creative_id} (e.g., "5_3")
        # - Legacy: ad_{campaign_id}_{creative_id} (e.g., "ad_5_3")
        try:
            parts = ad_id.split("_")
            if len(parts) >= 3 and parts[0] == "ad":
                # Legacy format: ad_{campaign_id}_{creative_id}
                return int(parts[1]), int(parts[2])
            elif len(parts) >= 2:
                # New format: {campaign_id}_{creative_id}
                return int(parts[0]), int(parts[1])
            else:
                return int(ad_id), None
        except (ValueError, IndexError):
            logger.warning(f"Invalid ad_id format: {ad_id}")
            return None, None

    def _get_event_type(self, event_type: str) -> int | None:
        """Convert event type string to enum."""
        mapping = {
            # Full names (legacy)
            "impression": EventType.IMPRESSION,
            "click": EventType.CLICK,
            "conversion": EventType.CONVERSION,
            # Short codes (legacy)
            "imp": EventType.IMPRESSION,
            "clk": EventType.CLICK,
            "conv": EventType.CONVERSION,
            # Minimal codes (current)
            "v": EventType.IMPRESSION,  # view
            "c": EventType.CLICK,
            "x": EventType.CONVERSION,
        }
        return mapping.get(event_type.lower())

    async def _calculate_cost(
        self,
        event_type: int,
        campaign_id: int | None,
    ) -> Decimal:
        """
        Calculate cost for the event based on campaign bid type.

        Cost rules:
        - CPM (bid_type=1): Charge bid_amount/1000 on impression events
        - CPC (bid_type=2): Charge bid_amount on click events
        - CPA (bid_type=3): Charge bid_amount on conversion events
        - OCPM (bid_type=4): Same as CPM for billing

        Also updates campaign.spent_today and campaign.spent_total.
        """
        if campaign_id is None:
            return Decimal("0.000000")

        # Lookup campaign
        result = await self.session.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )
        campaign = result.scalar_one_or_none()

        if campaign is None:
            logger.warning(f"Campaign not found for cost calculation: {campaign_id}")
            return Decimal("0.000000")

        # House ads have no cost
        if getattr(campaign, 'is_house_ad', False):
            logger.debug(f"Skipping cost for house ad campaign: {campaign_id}")
            return Decimal("0.000000")

        cost = Decimal("0.000000")

        # Calculate cost based on bid type and event type
        if campaign.bid_type == BidType.CPM or campaign.bid_type == BidType.OCPM:
            # CPM/OCPM: charge on impressions
            if event_type == EventType.IMPRESSION:
                cost = campaign.bid_amount / Decimal("1000")
        elif campaign.bid_type == BidType.CPC:
            # CPC: charge on clicks
            if event_type == EventType.CLICK:
                cost = campaign.bid_amount
        elif campaign.bid_type == BidType.CPA:
            # CPA: charge on conversions
            if event_type == EventType.CONVERSION:
                cost = campaign.bid_amount

        # Update campaign spend if there's a cost
        if cost > 0:
            campaign.spent_today = (campaign.spent_today or Decimal("0")) + cost
            campaign.spent_total = (campaign.spent_total or Decimal("0")) + cost
            logger.debug(
                "Cost calculated",
                campaign_id=campaign_id,
                event_type=event_type,
                cost=float(cost),
            )

        return cost

    async def _update_stats(self, campaign_id: int | None, event_type: int) -> None:
        """Update real-time statistics in Redis."""
        if campaign_id is None:
            return

        hour = current_hour()
        key = CacheKeys.stat_hourly(campaign_id, hour)

        # Increment appropriate counter
        if event_type == EventType.IMPRESSION:
            await redis_client.hincrby(key, "impressions", 1)
        elif event_type == EventType.CLICK:
            await redis_client.hincrby(key, "clicks", 1)
        elif event_type == EventType.CONVERSION:
            await redis_client.hincrby(key, "conversions", 1)

        # Set TTL (48 hours)
        await redis_client.expire(key, 48 * 3600)

    async def _update_frequency(self, user_id: str, campaign_id: int | None) -> None:
        """Update frequency counter."""
        if campaign_id is None:
            return

        today = current_date()
        hour = current_hour()

        # Update daily counter
        daily_key = CacheKeys.freq_daily(user_id, campaign_id, today)
        await redis_client.incr(daily_key)
        await redis_client.expire(daily_key, 24 * 3600)

        # Update hourly counter
        hourly_key = CacheKeys.freq_hourly(user_id, campaign_id, hour)
        await redis_client.incr(hourly_key)
        await redis_client.expire(hourly_key, 3600)
