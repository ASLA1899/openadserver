"""
Ad serving endpoints.
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from liteads.ad_server.routers.media import encode_url
from liteads.ad_server.services.ad_service import AdService
from liteads.common.config import get_settings
from liteads.common.database import get_session
from liteads.common.logger import get_logger, log_context
from liteads.common.utils import generate_request_id
from liteads.schemas.request import AdRequest
from liteads.schemas.response import AdListResponse, AdResponse, CreativeResponse, TrackingUrls

logger = get_logger(__name__)
router = APIRouter()

# Standard ad dimensions that blockers target
STANDARD_AD_SIZES = {
    (300, 250), (728, 90), (160, 600), (300, 600),
    (320, 50), (320, 100), (468, 60), (336, 280),
    (970, 90), (970, 250), (300, 50), (320, 480),
}


# Cache version - increment when proxy route/behavior changes to bust browser cache
PROXY_CACHE_VERSION = 2


def _get_page_url_from_referer(request: Request) -> str | None:
    """
    Extract page URL from HTTP Referer header.

    Used as fallback for domain targeting when page_url is not explicitly provided.
    Returns None if Referer is missing or invalid.
    """
    settings = get_settings()
    if not settings.ad_serving.use_referer_for_targeting:
        return None

    referer = request.headers.get("referer") or request.headers.get("referrer")
    if not referer:
        return None

    # Basic validation - must be a valid URL
    try:
        from urllib.parse import urlparse
        parsed = urlparse(referer)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return referer
    except Exception:
        pass

    return None


def _get_proxied_image_url(
    base_url: str,
    image_url: str | None,
    width: int | None,
    height: int | None,
) -> str | None:
    """
    Convert an image URL to a proxied URL.

    Offsets dimensions by 1 pixel if they match standard ad sizes
    to avoid dimension-based ad blocking.
    """
    if not image_url:
        return None

    # Encode the original URL
    encoded = encode_url(image_url)

    # Check if dimensions match standard ad sizes
    if width and height and (width, height) in STANDARD_AD_SIZES:
        # Offset by 1 pixel to avoid detection
        new_width = width - 1
        new_height = height - 1
        return f"{base_url}/res/{encoded}?w={new_width}&h={new_height}&v={PROXY_CACHE_VERSION}"

    # No resize needed, just proxy
    return f"{base_url}/res/{encoded}?v={PROXY_CACHE_VERSION}"


def get_ad_service(session: AsyncSession = Depends(get_session)) -> AdService:
    """Dependency to get ad service."""
    return AdService(session)


@router.post("/request", response_model=AdListResponse)
async def request_ads(
    request: Request,
    ad_request: AdRequest,
    ad_service: AdService = Depends(get_ad_service),
) -> AdListResponse:
    """
    Request ads for a given slot.

    This is the main ad serving endpoint. It:
    1. Retrieves candidate ads based on targeting
    2. Filters ads by budget, frequency, and quality
    3. Predicts CTR/CVR using ML models
    4. Ranks ads by eCPM
    5. Returns top ads with tracking URLs
    """
    request_id = generate_request_id()
    settings = get_settings()

    # Add request context for logging
    log_context(
        request_id=request_id,
        slot_id=ad_request.slot_id,
        user_id=ad_request.user_id,
    )

    logger.info(
        "Ad request received",
        num_requested=ad_request.num_ads,
        os=ad_request.device.os if ad_request.device else None,
    )

    # Get client IP from request
    client_ip = request.client.host if request.client else None
    if ad_request.geo and not ad_request.geo.ip:
        ad_request.geo.ip = client_ip

    # Use Referer header as fallback for page_url if not provided
    if not (ad_request.context and ad_request.context.page_url):
        referer_url = _get_page_url_from_referer(request)
        if referer_url:
            from liteads.schemas.request import ContextInfo
            if ad_request.context:
                ad_request.context.page_url = referer_url
            else:
                ad_request.context = ContextInfo(page_url=referer_url)

    # Serve ads
    candidates = await ad_service.serve_ads(
        request=ad_request,
        request_id=request_id,
    )

    # Build response
    ads = []
    base_url = str(request.base_url).rstrip("/")

    # Fix protocol for reverse proxy (check X-Forwarded-Proto header)
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    if forwarded_proto == "https" and base_url.startswith("http://"):
        base_url = "https://" + base_url[7:]

    for candidate in candidates[: ad_request.num_ads]:
        # Build tracking URLs with obfuscated parameters to avoid ad blockers
        # Format: /api/v1/px/?t={type}&r={request_id}&i={campaign_id}_{creative_id}
        ad_id = f"{candidate.campaign_id}_{candidate.creative_id}"
        tracking = TrackingUrls(
            impression_url=f"{base_url}/api/v1/px/?t=v&r={request_id}&i={ad_id}",
            click_url=f"{base_url}/api/v1/px/?t=c&r={request_id}&i={ad_id}",
            conversion_url=f"{base_url}/api/v1/px/?t=x&r={request_id}&i={ad_id}",
        )

        # Build creative response with proxied image URL
        proxied_image_url = _get_proxied_image_url(
            base_url,
            candidate.image_url,
            candidate.width,
            candidate.height,
        )

        # Adjust dimensions if they were resized
        creative_width = candidate.width
        creative_height = candidate.height
        if candidate.width and candidate.height:
            if (candidate.width, candidate.height) in STANDARD_AD_SIZES:
                creative_width = candidate.width - 1
                creative_height = candidate.height - 1

        creative = CreativeResponse(
            title=candidate.title,
            description=candidate.description,
            image_url=proxied_image_url,
            video_url=candidate.video_url,
            landing_url=candidate.landing_url,
            width=creative_width,
            height=creative_height,
            creative_type=_get_creative_type_name(candidate.creative_type),
        )

        ad = AdResponse(
            ad_id=ad_id,
            campaign_id=candidate.campaign_id,
            creative_id=candidate.creative_id,
            creative=creative,
            tracking=tracking,
            metadata={
                "ecpm": round(candidate.ecpm, 4),
                "pctr": round(candidate.pctr, 6),
            }
            if settings.debug
            else None,
        )
        ads.append(ad)

    logger.info(
        "Ad request completed",
        num_returned=len(ads),
    )

    return AdListResponse(
        request_id=request_id,
        ads=ads,
        count=len(ads),
    )


def _get_creative_type_name(creative_type: int) -> str:
    """Convert creative type enum to string."""
    types = {1: "banner", 2: "native", 3: "video", 4: "interstitial"}
    return types.get(creative_type, "banner")


@router.get("/serve")
async def serve_ad_image(
    request: Request,
    slot: str = Query(..., description="Slot ID (e.g., 'leaderboard-728x90')"),
    ad_service: AdService = Depends(get_ad_service),
) -> RedirectResponse:
    """
    Simple image endpoint - returns a redirect to the ad image.

    Usage in CMS (with click tracking):
        <a href="https://media.aslalabs.org/api/v1/ad/click?slot=leaderboard-728x90">
          <img src="https://media.aslalabs.org/api/v1/ad/serve?slot=leaderboard-728x90" width="728" height="90" />
        </a>

    Usage (image only, no click tracking):
        <img src="https://media.aslalabs.org/api/v1/ad/serve?slot=leaderboard-728x90" width="728" height="90" />

    Note: Each page load gets a new ad. Impression is tracked when image loads.
    """
    request_id = generate_request_id()

    # Parse dimensions from slot ID
    import re
    dim_match = re.search(r"(\d+)x(\d+)", slot)
    width = int(dim_match.group(1)) if dim_match else 300
    height = int(dim_match.group(2)) if dim_match else 250

    # Build ad request with Referer for domain targeting
    from liteads.schemas.request import ContextInfo, DeviceInfo
    page_url = _get_page_url_from_referer(request)
    ad_request = AdRequest(
        slot_id=slot,
        num_ads=1,
        device=DeviceInfo(os="web"),
        context=ContextInfo(page_url=page_url) if page_url else None,
    )

    candidates = await ad_service.serve_ads(request=ad_request, request_id=request_id)

    if not candidates:
        # Return a transparent 1x1 pixel if no ads
        return RedirectResponse(
            url="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7",
            status_code=302,
        )

    candidate = candidates[0]
    ad_id = f"{candidate.campaign_id}_{candidate.creative_id}"

    # Build base URL for impression tracking
    base_url = str(request.base_url).rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    if forwarded_proto == "https" and base_url.startswith("http://"):
        base_url = "https://" + base_url[7:]

    # Get proxied image URL
    proxied_image_url = _get_proxied_image_url(
        base_url, candidate.image_url, candidate.width, candidate.height
    )

    # Track impression
    from liteads.ad_server.services.event_service import EventService
    event_service = EventService(ad_service.session)
    await event_service.track_event(request_id, ad_id, "impression")
    await ad_service.session.commit()

    # Store the ad_id in a cookie or header so click tracking knows which ad was shown
    response = RedirectResponse(url=proxied_image_url, status_code=302)
    response.set_cookie(
        key=f"ad_{slot}",
        value=f"{request_id}:{ad_id}",
        max_age=3600,
        httponly=True,
        samesite="none",
        secure=True,
    )
    # Prevent caching so each request gets fresh ad
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/click")
async def track_click_and_redirect(
    request: Request,
    slot: str = Query(..., description="Slot ID"),
    ad_service: AdService = Depends(get_ad_service),
) -> RedirectResponse:
    """
    Click tracking endpoint - tracks click and redirects to landing page.

    Used with /serve endpoint for simple image-based ads.
    """
    # Get the ad info from cookie
    cookie_value = request.cookies.get(f"ad_{slot}")

    if not cookie_value or ":" not in cookie_value:
        # No cookie - try to serve a fresh ad and redirect to its landing page
        from liteads.schemas.request import DeviceInfo
        request_id = generate_request_id()
        ad_request = AdRequest(
            slot_id=slot,
            num_ads=1,
            device=DeviceInfo(os="web"),
        )
        candidates = await ad_service.serve_ads(request=ad_request, request_id=request_id)
        if candidates:
            return RedirectResponse(url=candidates[0].landing_url, status_code=302)
        return RedirectResponse(url="/", status_code=302)

    request_id, ad_id = cookie_value.split(":", 1)

    # Track the click
    from liteads.ad_server.services.event_service import EventService
    event_service = EventService(ad_service.session)
    await event_service.track_event(request_id, ad_id, "click")
    await ad_service.session.commit()

    # Get the landing URL for this ad
    campaign_id, creative_id = ad_id.split("_")
    from sqlalchemy import select
    from liteads.models import Creative
    result = await ad_service.session.execute(
        select(Creative).where(Creative.id == int(creative_id))
    )
    creative = result.scalar_one_or_none()

    if creative and creative.landing_url:
        return RedirectResponse(url=creative.landing_url, status_code=302)

    return RedirectResponse(url="/", status_code=302)


@router.get("/go")
async def click_through(
    request: Request,
    r: str = Query(..., description="Request ID"),
    i: str = Query(..., description="Ad ID (campaign_creative)"),
    ad_service: AdService = Depends(get_ad_service),
) -> RedirectResponse:
    """
    Click-through endpoint - tracks click and redirects to landing page.

    Used by embed endpoint for proper click tracking with redirect.
    """
    # Track the click
    from liteads.ad_server.services.event_service import EventService
    event_service = EventService(ad_service.session)
    await event_service.track_event(r, i, "click")
    await ad_service.session.commit()

    # Get the landing URL for this ad
    try:
        campaign_id, creative_id = i.split("_")
        from sqlalchemy import select
        from liteads.models import Creative
        result = await ad_service.session.execute(
            select(Creative).where(Creative.id == int(creative_id))
        )
        creative = result.scalar_one_or_none()

        if creative and creative.landing_url:
            return RedirectResponse(url=creative.landing_url, status_code=302)
    except (ValueError, Exception):
        pass

    return RedirectResponse(url="/", status_code=302)


@router.get("/embed", response_class=HTMLResponse)
async def embed_ad(
    request: Request,
    slot: str = Query(..., description="Slot ID (e.g., 'leaderboard-728x90')"),
    ad_service: AdService = Depends(get_ad_service),
) -> HTMLResponse:
    """
    Embed endpoint for simple iframe/image integration.

    Usage in CMS:
        <iframe src="https://media.aslalabs.org/api/v1/ad/embed?slot=leaderboard-728x90"
                width="728" height="90" frameborder="0" scrolling="no"></iframe>

    This endpoint returns a minimal HTML page with the ad that can be embedded
    in any CMS or webpage using a simple iframe tag.
    """
    request_id = generate_request_id()
    settings = get_settings()

    # Parse dimensions from slot ID if present (e.g., "leaderboard-728x90" -> 728, 90)
    import re
    dim_match = re.search(r"(\d+)x(\d+)", slot)
    width = int(dim_match.group(1)) if dim_match else 300
    height = int(dim_match.group(2)) if dim_match else 250

    # Build a minimal ad request with default device info
    # Use Referer header for domain targeting when available
    from liteads.schemas.request import ContextInfo, DeviceInfo
    page_url = _get_page_url_from_referer(request)
    ad_request = AdRequest(
        slot_id=slot,
        num_ads=1,
        device=DeviceInfo(os="web"),
        context=ContextInfo(page_url=page_url) if page_url else None,
    )

    # Get client IP
    client_ip = request.client.host if request.client else None
    if ad_request.geo and not ad_request.geo.ip:
        ad_request.geo.ip = client_ip

    # Serve ads
    candidates = await ad_service.serve_ads(
        request=ad_request,
        request_id=request_id,
    )

    # Build base URL
    base_url = str(request.base_url).rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    if forwarded_proto == "https" and base_url.startswith("http://"):
        base_url = "https://" + base_url[7:]

    if not candidates:
        # Return empty/transparent placeholder if no ads
        return HTMLResponse(
            content=f"""<!DOCTYPE html>
<html><head><style>body{{margin:0;padding:0;}}</style></head>
<body><div style="width:{width}px;height:{height}px;"></div></body></html>""",
            status_code=200,
        )

    candidate = candidates[0]
    ad_id = f"{candidate.campaign_id}_{candidate.creative_id}"

    # Build URLs
    impression_url = f"{base_url}/api/v1/px/?t=v&r={request_id}&i={ad_id}"
    click_url = f"{base_url}/api/v1/ad/go?r={request_id}&i={ad_id}"
    proxied_image_url = _get_proxied_image_url(
        base_url,
        candidate.image_url,
        candidate.width,
        candidate.height,
    )

    # Adjust dimensions for display
    display_width = candidate.width or width
    display_height = candidate.height or height
    if display_width and display_height and (display_width, display_height) in STANDARD_AD_SIZES:
        display_width -= 1
        display_height -= 1

    # Return minimal HTML with ad
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ margin: 0; padding: 0; overflow: hidden; }}
a {{ display: block; }}
img {{ display: block; max-width: 100%; height: auto; }}
</style>
</head>
<body>
<a href="{click_url}" target="_top" rel="noopener">
<img src="{proxied_image_url}" width="{display_width}" height="{display_height}" alt="{candidate.title or 'Advertisement'}" />
</a>
<img src="{impression_url}" width="1" height="1" style="position:absolute;left:-9999px;" alt="" />
</body>
</html>"""

    return HTMLResponse(content=html, status_code=200)
