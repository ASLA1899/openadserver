"""
Event tracking endpoints.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.ad_server.services.event_service import EventService
from liteads.common.database import get_session
from liteads.common.logger import get_logger, log_context
from liteads.common.utils import current_timestamp
from liteads.schemas.request import EventRequest
from liteads.schemas.response import EventResponse

logger = get_logger(__name__)
router = APIRouter()


def get_event_service(session: AsyncSession = Depends(get_session)) -> EventService:
    """Dependency to get event service."""
    return EventService(session)


@router.post("/track", response_model=EventResponse)
async def track_event(
    event: EventRequest,
    event_service: EventService = Depends(get_event_service),
) -> EventResponse:
    """
    Track an ad event (impression, click, or conversion).

    Events are used for:
    - Billing calculation
    - Performance metrics
    - ML model training data
    """
    log_context(
        request_id=event.request_id,
        ad_id=event.ad_id,
        event_type=event.event_type,
    )

    logger.info("Event received")

    # Record the event
    success = await event_service.track_event(
        request_id=event.request_id,
        ad_id=event.ad_id,
        event_type=event.event_type,
        user_id=event.user_id,
        timestamp=event.timestamp or current_timestamp(),
        extra=event.extra,
    )

    return EventResponse(
        success=success,
        message="Event recorded" if success else "Failed to record event",
    )


@router.get("/")
async def track_event_get(
    t: str = Query(..., description="Event type (v=view, c=click, x=conversion)"),
    r: str = Query(..., description="Request ID"),
    i: str = Query(..., description="ID"),
    event_service: EventService = Depends(get_event_service),
) -> EventResponse:
    """
    Track event via GET request (for pixel tracking).

    This endpoint is used for tracking URLs embedded in ads.
    """
    log_context(
        request_id=r,
        ad_id=i,
        event_type=t,
    )

    logger.info("Pixel event received")

    success = await event_service.track_event(
        request_id=r,
        ad_id=i,
        event_type=t,
        user_id=None,
        timestamp=current_timestamp(),
        extra=None,
    )

    return EventResponse(
        success=success,
        message="Event recorded" if success else "Failed to record event",
    )
