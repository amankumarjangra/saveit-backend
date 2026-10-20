"""
SaveIt Backend - URL validation and platform detection utilities.

Handles URL cleaning (removing tracking params), platform detection via
regex, and domain allowlisting to prevent SSRF attacks.
"""

from __future__ import annotations

import re
from ipaddress import ip_address
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from app.schemas import Platform


# ──────────────────────────────────────────────
# Domain Allowlists
# ──────────────────────────────────────────────

INSTAGRAM_DOMAINS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
}

YOUTUBE_DOMAINS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

ALLOWED_DOMAINS = INSTAGRAM_DOMAINS | YOUTUBE_DOMAINS

# ──────────────────────────────────────────────
# Tracking Parameter Removal
# ──────────────────────────────────────────────

TRACKING_PARAMS = {
    # Instagram
    "igsh", "igshid", "ig_rid", "ig_mid",
    # YouTube
    "si", "feature", "pp", "cbrd", "ucbcb",
    # Generic
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "ref_src", "ref_url",
}

# ──────────────────────────────────────────────
# URL Patterns
# ──────────────────────────────────────────────

INSTAGRAM_PATH_PATTERN = re.compile(
    r"^/(reel|reels|p|tv)/[A-Za-z0-9_-]+/?$"
)

YOUTUBE_VIDEO_PATTERN = re.compile(
    r"^/watch$"  # Must have ?v= query param
)

YOUTUBE_SHORTS_PATTERN = re.compile(
    r"^/shorts/[A-Za-z0-9_-]+/?$"
)

YOUTUBE_EMBED_PATTERN = re.compile(
    r"^/embed/[A-Za-z0-9_-]+/?$"
)

YOUTU_BE_PATTERN = re.compile(
    r"^/[A-Za-z0-9_-]+/?$"
)

# ──────────────────────────────────────────────
# Private / Loopback IP Detection (SSRF protection)
# ──────────────────────────────────────────────

def _is_private_or_loopback(hostname: str) -> bool:
    """Check if hostname resolves to a private or loopback IP address."""
    try:
        addr = ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_reserved
    except ValueError:
        # Not an IP literal — this is a normal domain name, which is fine.
        return False


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def clean_url(raw_url: str) -> str:
    """
    Sanitize a URL by:
    1. Stripping whitespace.
    2. Removing tracking query parameters.
    3. Normalizing the URL structure.
    """
    url = raw_url.strip()
    parsed = urlparse(url)

    # Remove tracking params
    query_params = parse_qs(parsed.query, keep_blank_values=False)
    cleaned_params = {
        k: v for k, v in query_params.items()
        if k.lower() not in TRACKING_PARAMS
    }
    cleaned_query = urlencode(cleaned_params, doseq=True)

    # Rebuild URL
    cleaned = urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path.rstrip("/") + "/" if parsed.path and parsed.path != "/" else parsed.path,
        parsed.params,
        cleaned_query,
        "",  # Drop fragment
    ))

    return cleaned


def detect_platform(url: str) -> Platform:
    """Detect which platform a URL belongs to."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if hostname in INSTAGRAM_DOMAINS:
        return Platform.INSTAGRAM
    elif hostname in YOUTUBE_DOMAINS:
        return Platform.YOUTUBE
    else:
        return Platform.UNKNOWN


def validate_url(raw_url: str) -> tuple[bool, str, Platform]:
    """
    Validate and clean a URL.

    Returns:
        (is_valid, cleaned_url_or_error_message, platform)
    """
    url = raw_url.strip()

    # Basic scheme check
    if not url.startswith(("http://", "https://")):
        return False, "URL must start with http:// or https://", Platform.UNKNOWN

    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format.", Platform.UNKNOWN

    hostname = parsed.hostname or ""

    # SSRF protection: block private/loopback IPs
    if _is_private_or_loopback(hostname):
        return False, "Invalid URL.", Platform.UNKNOWN

    # Domain allowlist check
    if hostname not in ALLOWED_DOMAINS:
        return False, "This platform is not supported yet.", Platform.UNKNOWN

    # Platform-specific path validation
    platform = detect_platform(url)

    if platform == Platform.INSTAGRAM:
        if not INSTAGRAM_PATH_PATTERN.match(parsed.path):
            return False, "Invalid Instagram URL. Please share a Reel, Post, or IGTV link.", Platform.INSTAGRAM

    elif platform == Platform.YOUTUBE:
        path = parsed.path
        if hostname in {"youtu.be", "www.youtu.be"}:
            if not YOUTU_BE_PATTERN.match(path):
                return False, "Invalid YouTube short URL.", Platform.YOUTUBE
        elif YOUTUBE_VIDEO_PATTERN.match(path):
            query_params = parse_qs(parsed.query)
            if "v" not in query_params:
                return False, "YouTube URL must contain a video ID (?v=...).", Platform.YOUTUBE
        elif YOUTUBE_SHORTS_PATTERN.match(path):
            pass  # Valid shorts URL
        elif YOUTUBE_EMBED_PATTERN.match(path):
            pass  # Valid embed URL
        else:
            return False, "Invalid YouTube URL. Please share a video, Shorts, or embed link.", Platform.YOUTUBE

    # Clean the URL
    cleaned = clean_url(url)

    return True, cleaned, platform
