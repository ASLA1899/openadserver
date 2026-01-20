"""
Media proxy for serving creative assets.

Proxies images through the ad server to:
1. Obfuscate original URLs (avoid ad blocker patterns)
2. Optionally resize images (avoid standard ad dimensions)
3. Cache images for performance
"""

import base64
import hashlib
from io import BytesIO
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from liteads.common.cache import redis_client
from liteads.common.logger import get_logger

logger = get_logger(__name__)
router = APIRouter()

# Cache TTL: 1 hour
CACHE_TTL = 3600

# Max image size to proxy (10MB)
MAX_IMAGE_SIZE = 10 * 1024 * 1024

# Allowed image content types
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/svg+xml",
}


def encode_url(url: str) -> str:
    """Encode a URL to a URL-safe base64 string."""
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def decode_url(encoded: str) -> str:
    """Decode a URL-safe base64 string back to URL."""
    # Add back padding
    padding = 4 - len(encoded) % 4
    if padding != 4:
        encoded += "=" * padding
    try:
        return base64.urlsafe_b64decode(encoded.encode()).decode()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid encoded URL")


def get_cache_key(encoded_url: str, width: Optional[int], height: Optional[int]) -> str:
    """Generate a cache key for the image."""
    size_suffix = f"_{width}x{height}" if width and height else ""
    return f"media:img:{encoded_url}{size_suffix}"


async def fetch_image(url: str) -> tuple[bytes, str]:
    """Fetch image from the original URL."""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            if content_type not in ALLOWED_CONTENT_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Content type not allowed: {content_type}",
                )

            if len(response.content) > MAX_IMAGE_SIZE:
                raise HTTPException(status_code=400, detail="Image too large")

            return response.content, content_type

    except httpx.HTTPStatusError as e:
        logger.warning(f"Failed to fetch image: {url}, status: {e.response.status_code}")
        raise HTTPException(status_code=502, detail="Failed to fetch image")
    except httpx.RequestError as e:
        logger.warning(f"Request error fetching image: {url}, error: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch image")


def resize_image(
    image_data: bytes,
    content_type: str,
    width: int,
    height: int,
) -> tuple[bytes, str]:
    """Resize image to specified dimensions."""
    try:
        from PIL import Image

        # Open image
        img = Image.open(BytesIO(image_data))

        # Resize
        img = img.resize((width, height), Image.Resampling.LANCZOS)

        # Save to buffer
        output = BytesIO()

        # Determine format from content type
        format_map = {
            "image/jpeg": "JPEG",
            "image/png": "PNG",
            "image/gif": "GIF",
            "image/webp": "WEBP",
        }
        img_format = format_map.get(content_type, "PNG")

        # Handle RGBA for JPEG
        if img_format == "JPEG" and img.mode == "RGBA":
            img = img.convert("RGB")

        img.save(output, format=img_format, quality=90)
        output.seek(0)

        return output.read(), content_type

    except ImportError:
        logger.warning("Pillow not installed, returning original image")
        return image_data, content_type
    except Exception as e:
        logger.warning(f"Failed to resize image: {e}")
        return image_data, content_type


@router.get("/{encoded_url:path}")
async def proxy_image(
    encoded_url: str,
    w: Optional[int] = Query(None, ge=1, le=2000, description="Target width"),
    h: Optional[int] = Query(None, ge=1, le=2000, description="Target height"),
) -> Response:
    """
    Proxy and optionally resize an image.

    The URL is base64url encoded to avoid ad blocker pattern matching.
    Optional w and h parameters resize the image to avoid standard ad dimensions.
    """
    # Decode the original URL
    original_url = decode_url(encoded_url)

    # Check cache first
    cache_key = get_cache_key(encoded_url, w, h)
    cached = await redis_client.get(cache_key)

    if cached:
        # Cache stores: content_type:base64_image_data
        try:
            content_type, image_b64 = cached.split(":", 1)
            image_data = base64.b64decode(image_b64)
            logger.debug(f"Cache hit for image: {cache_key}")
            return Response(
                content=image_data,
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "X-Cache": "HIT",
                },
            )
        except Exception:
            # Invalid cache entry, fetch fresh
            pass

    # Fetch the original image
    image_data, content_type = await fetch_image(original_url)

    # Resize if dimensions specified
    if w and h:
        image_data, content_type = resize_image(image_data, content_type, w, h)

    # Cache the result
    try:
        cache_value = f"{content_type}:{base64.b64encode(image_data).decode()}"
        await redis_client.set(cache_key, cache_value, ttl=CACHE_TTL)
    except Exception as e:
        logger.warning(f"Failed to cache image: {e}")

    return Response(
        content=image_data,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Cache": "MISS",
        },
    )
