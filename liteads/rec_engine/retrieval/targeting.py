"""
Targeting-based retrieval.

Retrieves ads based on targeting rules matching user attributes.
"""

import fnmatch
import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.common.cache import CacheKeys, redis_client
from liteads.common.logger import get_logger
from liteads.common.utils import json_dumps, json_loads
from liteads.models import Campaign, Creative, Status, TargetingRule
from liteads.rec_engine.retrieval.base import BaseRetrieval
from liteads.schemas.internal import AdCandidate, UserContext

logger = get_logger(__name__)


class TargetingRetrieval(BaseRetrieval):
    """
    Retrieval based on targeting rules.

    Matches user attributes against campaign targeting rules to find
    eligible ads.
    """

    # Pattern to extract dimensions from slot_id (e.g., "leaderboard-728x90" → 728, 90)
    _DIMENSION_PATTERN = re.compile(r"(\d+)x(\d+)")

    def __init__(self, session: AsyncSession):
        self.session = session
        self._cache_ttl = 300  # 5 minutes

    @staticmethod
    def _parse_slot_dimensions(slot_id: str) -> tuple[int, int] | None:
        """
        Parse width and height from slot_id.

        Examples:
            "leaderboard-728x90" → (728, 90)
            "sidebar-300x250" → (300, 250)
            "homepage-banner" → None (no dimensions found)
        """
        match = TargetingRetrieval._DIMENSION_PATTERN.search(slot_id)
        if match:
            return int(match.group(1)), int(match.group(2))
        return None

    @staticmethod
    def _creative_matches_slot(
        creative_data: dict[str, Any],
        slot_dimensions: tuple[int, int] | None,
    ) -> bool:
        """Check if creative dimensions match the slot dimensions."""
        if slot_dimensions is None:
            return True  # No slot dimensions = accept any creative

        slot_width, slot_height = slot_dimensions
        creative_width = creative_data.get("width")
        creative_height = creative_data.get("height")

        # If creative has no dimensions, accept it (legacy support)
        if creative_width is None or creative_height is None:
            return True

        return creative_width == slot_width and creative_height == slot_height

    @staticmethod
    def _extract_domain(url: str) -> str | None:
        """
        Extract domain from URL.

        Examples:
            "https://www.asla.org/page" → "asla.org"
            "https://blog.example.com/post" → "example.com"
        """
        if not url:
            return None
        try:
            parsed = urlparse(url)
            host = parsed.netloc.lower()
            # Remove www. prefix
            if host.startswith("www."):
                host = host[4:]
            # Extract base domain (last two parts for most TLDs)
            parts = host.split(".")
            if len(parts) >= 2:
                return ".".join(parts[-2:])
            return host
        except Exception:
            return None

    @staticmethod
    def _match_domain_targeting(
        target_domains: list[str] | str | None,
        page_url: str | None,
    ) -> bool:
        """Check if page URL domain matches target domains."""
        if not target_domains:
            return True  # No domain targeting = match all

        if not page_url:
            return True  # No page URL = can't filter, allow

        # Parse JSON string if needed
        if isinstance(target_domains, str):
            try:
                target_domains = json_loads(target_domains)
            except (ValueError, TypeError):
                return True  # Invalid JSON, skip filtering

        # After parsing, check if it's a valid list
        if not target_domains or not isinstance(target_domains, list):
            return True

        page_domain = TargetingRetrieval._extract_domain(page_url)
        if not page_domain:
            return True  # Can't parse domain = allow

        # Check if page domain matches any target domain
        for target in target_domains:
            target_clean = target.lower().strip()
            if target_clean.startswith("www."):
                target_clean = target_clean[4:]
            if page_domain == target_clean:
                return True
            # Also check if target is a subdomain match
            if page_domain.endswith("." + target_clean):
                return True

        return False

    async def retrieve(
        self,
        user_context: UserContext,
        slot_id: str,
        limit: int = 100,
        **kwargs: Any,
    ) -> list[AdCandidate]:
        """
        Retrieve candidates matching targeting rules.

        Flow:
        1. Get all active campaigns with creatives
        2. For each campaign, check targeting rules and page targeting
        3. Separate paid ads from house ads
        4. Return paid ads, or house ads as fallback
        """
        # Get active campaigns (with caching)
        campaigns = await self._get_active_campaigns()

        if not campaigns:
            logger.debug("No active campaigns found")
            return []

        # Parse slot dimensions for filtering
        slot_dimensions = self._parse_slot_dimensions(slot_id)
        if slot_dimensions:
            logger.debug(f"Slot {slot_id} requires dimensions: {slot_dimensions[0]}x{slot_dimensions[1]}")

        paid_candidates: list[AdCandidate] = []
        house_candidates: list[AdCandidate] = []

        # Get page URL from context if provided
        page_url = kwargs.get("page_url") or (
            user_context.page_url if hasattr(user_context, "page_url") else None
        )

        for campaign_data in campaigns:
            # Check if campaign matches user targeting
            if not self._match_targeting(campaign_data, user_context):
                continue

            # Check page targeting
            if not self._match_page_targeting(campaign_data, page_url):
                continue

            # Check domain targeting
            target_domains = campaign_data.get("target_domains")
            if not self._match_domain_targeting(target_domains, page_url):
                continue

            is_house_ad = campaign_data.get("is_house_ad", False)

            # Create candidate for each creative that matches slot dimensions
            for creative_data in campaign_data.get("creatives", []):
                # Filter by slot dimensions
                if not self._creative_matches_slot(creative_data, slot_dimensions):
                    continue
                candidate = AdCandidate(
                    campaign_id=campaign_data["id"],
                    creative_id=creative_data["id"],
                    advertiser_id=campaign_data["advertiser_id"],
                    bid=campaign_data["bid_amount"],
                    bid_type=campaign_data["bid_type"],
                    priority_boost=campaign_data.get("priority_boost", 1.0),
                    title=creative_data.get("title"),
                    description=creative_data.get("description"),
                    image_url=creative_data.get("image_url"),
                    video_url=creative_data.get("video_url"),
                    landing_url=creative_data.get("landing_url", ""),
                    creative_type=creative_data.get("creative_type", 1),
                    width=creative_data.get("width"),
                    height=creative_data.get("height"),
                    is_house_ad=is_house_ad,
                )

                if is_house_ad:
                    house_candidates.append(candidate)
                else:
                    paid_candidates.append(candidate)

                if len(paid_candidates) >= limit and len(house_candidates) >= limit:
                    break

            if len(paid_candidates) >= limit and len(house_candidates) >= limit:
                break

        # Return paid candidates if available, otherwise use house ads as fallback
        if paid_candidates:
            logger.debug(f"Retrieved {len(paid_candidates)} paid candidates")
            return paid_candidates[:limit]
        elif house_candidates:
            logger.debug(f"No paid ads, using {len(house_candidates)} house ads as fallback")
            return house_candidates[:limit]
        else:
            logger.debug("No candidates found (paid or house)")
            return []

    def _match_page_targeting(
        self,
        campaign_data: dict[str, Any],
        page_url: str | None,
    ) -> bool:
        """Check if page URL matches campaign page targeting rules."""
        page_targeting = campaign_data.get("page_targeting")

        if not page_targeting:
            return True  # No page targeting = match all pages

        if not page_url:
            return True  # No page URL provided = can't filter

        # Parse JSON string if needed
        if isinstance(page_targeting, str):
            try:
                page_targeting = json_loads(page_targeting)
            except (ValueError, TypeError):
                return True  # Invalid JSON, skip filtering

        # After parsing, check if it's a valid dict
        if not page_targeting or not isinstance(page_targeting, dict):
            return True

        include_patterns = page_targeting.get("include", [])
        exclude_patterns = page_targeting.get("exclude", [])

        # Check exclude patterns first (if matched, reject)
        for pattern in exclude_patterns:
            if fnmatch.fnmatch(page_url, pattern):
                return False

        # If include patterns exist, must match at least one
        if include_patterns:
            for pattern in include_patterns:
                if fnmatch.fnmatch(page_url, pattern):
                    return True
            return False  # Didn't match any include pattern

        return True  # No include patterns = match all (after exclude check)

    async def _get_active_campaigns(self) -> list[dict[str, Any]]:
        """Get all active campaigns with creatives and targeting rules."""
        # Try cache first
        cache_key = CacheKeys.active_ads()
        cached = await redis_client.get(cache_key)

        if cached:
            try:
                return json_loads(cached)
            except Exception:
                pass

        # Query from database
        stmt = (
            select(Campaign)
            .where(Campaign.status == Status.ACTIVE)
            .limit(1000)
        )

        result = await self.session.execute(stmt)
        campaigns = result.scalars().all()

        campaign_list: list[dict[str, Any]] = []

        for campaign in campaigns:
            if not campaign.is_active:
                continue

            campaign_data: dict[str, Any] = {
                "id": campaign.id,
                "advertiser_id": campaign.advertiser_id,
                "name": campaign.name,
                "bid_type": campaign.bid_type,
                "bid_amount": float(campaign.bid_amount),
                "priority_boost": float(campaign.priority_boost) if campaign.priority_boost else 1.0,
                "budget_daily": float(campaign.budget_daily) if campaign.budget_daily else None,
                "budget_total": float(campaign.budget_total) if campaign.budget_total else None,
                "spent_today": float(campaign.spent_today),
                "spent_total": float(campaign.spent_total),
                "freq_cap_daily": campaign.freq_cap_daily,
                "freq_cap_hourly": campaign.freq_cap_hourly,
                "is_house_ad": getattr(campaign, 'is_house_ad', False),
                "page_targeting": getattr(campaign, 'page_targeting', None),
                "target_domains": getattr(campaign, 'target_domains', None),
                "creatives": [],
                "targeting_rules": [],
            }

            # Add creatives
            for creative in campaign.creatives:
                if creative.status == Status.ACTIVE:
                    campaign_data["creatives"].append({
                        "id": creative.id,
                        "title": creative.title,
                        "description": creative.description,
                        "image_url": creative.image_url,
                        "video_url": creative.video_url,
                        "landing_url": creative.landing_url,
                        "creative_type": creative.creative_type,
                        "width": creative.width,
                        "height": creative.height,
                    })

            # Add targeting rules
            for rule in campaign.targeting_rules:
                campaign_data["targeting_rules"].append({
                    "rule_type": rule.rule_type,
                    "rule_value": rule.rule_value,
                    "is_include": rule.is_include,
                })

            if campaign_data["creatives"]:  # Only add if has active creatives
                campaign_list.append(campaign_data)

        # Cache the result
        if campaign_list:
            await redis_client.set(
                cache_key,
                json_dumps(campaign_list),
                ttl=self._cache_ttl,
            )

        return campaign_list

    def _match_targeting(
        self,
        campaign_data: dict[str, Any],
        user_context: UserContext,
    ) -> bool:
        """Check if user matches campaign targeting rules."""
        targeting_rules = campaign_data.get("targeting_rules", [])

        if not targeting_rules:
            return True  # No targeting = match all

        for rule in targeting_rules:
            rule_type = rule["rule_type"]
            rule_value = rule["rule_value"]
            is_include = rule["is_include"]

            matched = self._match_rule(rule_type, rule_value, user_context)

            # Include rule: must match
            # Exclude rule: must not match
            if is_include and not matched:
                return False
            if not is_include and matched:
                return False

        return True

    def _match_rule(
        self,
        rule_type: str,
        rule_value: dict[str, Any],
        user_context: UserContext,
    ) -> bool:
        """Match a single targeting rule against user context."""
        if rule_type == "age":
            if user_context.age is None:
                return True  # Unknown age matches
            min_age = rule_value.get("min", 0)
            max_age = rule_value.get("max", 999)
            return min_age <= user_context.age <= max_age

        elif rule_type == "gender":
            if user_context.gender is None:
                return True
            values = rule_value.get("values", [])
            return user_context.gender.lower() in [v.lower() for v in values]

        elif rule_type == "geo":
            countries = rule_value.get("countries", [])
            cities = rule_value.get("cities", [])

            if countries and user_context.country:
                if user_context.country.upper() not in [c.upper() for c in countries]:
                    return False

            if cities and user_context.city:
                if user_context.city.lower() not in [c.lower() for c in cities]:
                    return False

            return True

        elif rule_type == "device":
            device_types = rule_value.get("types", [])
            # Simplified device type detection
            if user_context.device_model:
                model_lower = user_context.device_model.lower()
                if "tablet" in model_lower or "pad" in model_lower:
                    device_type = "tablet"
                else:
                    device_type = "phone"

                if device_types and device_type not in device_types:
                    return False

            return True

        elif rule_type == "os":
            os_values = rule_value.get("values", [])
            if os_values and user_context.os:
                if user_context.os.lower() not in [v.lower() for v in os_values]:
                    return False
            return True

        elif rule_type == "interest":
            interests = rule_value.get("values", [])
            if interests and user_context.interests:
                # Match if user has any of the target interests
                user_interests_lower = [i.lower() for i in user_context.interests]
                target_interests_lower = [i.lower() for i in interests]
                if not any(i in user_interests_lower for i in target_interests_lower):
                    return False
            return True

        elif rule_type == "app_category":
            categories = rule_value.get("values", [])
            if categories and user_context.app_categories:
                user_cats_lower = [c.lower() for c in user_context.app_categories]
                target_cats_lower = [c.lower() for c in categories]
                if not any(c in user_cats_lower for c in target_cats_lower):
                    return False
            return True

        # Unknown rule type - default match
        return True

    async def refresh(self) -> None:
        """Clear cache to force refresh."""
        cache_key = CacheKeys.active_ads()
        await redis_client.delete(cache_key)
        logger.info("Targeting retrieval cache refreshed")
