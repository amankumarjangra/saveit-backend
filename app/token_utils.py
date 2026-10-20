"""
SaveIt Backend - HMAC Token utility for securing download URLs.

Generates short-lived signed tokens that are issued with /resolve responses
and validated on /download requests. This prevents hotlinking and abuse
without requiring any database.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from app.config import get_settings
from app.url_utils import clean_url


def generate_download_token(url: str, format_id: str = "any", item_id: str = "0") -> str:
    """
    Generate an HMAC-signed, time-limited download token.

    The token encodes: normalized URL (without trailing slash) + expiry timestamp.
    It is valid for `token_expiry_seconds` from creation.
    """
    settings = get_settings()
    expiry = int(time.time()) + settings.token_expiry_seconds
    norm_url = clean_url(url).rstrip("/")
    payload = f"{norm_url}|{expiry}"
    signature = hmac.new(
        settings.secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # Token format: expiry:signature
    return f"{expiry}:{signature}"


def verify_download_token(
    token: str, url: str, format_id: str = "any", item_id: str = "0"
) -> bool:
    """
    Verify an HMAC-signed download token.

    Returns True if the token is valid and not expired.
    Tolerates URL variations (trailing slash, tracking parameters)
    and any selected format_id.
    """
    settings = get_settings()

    try:
        parts = token.split(":", 1)
        if len(parts) != 2:
            return False

        expiry_str, provided_signature = parts
        expiry = int(expiry_str)

        # Check expiry
        if time.time() > expiry:
            return False

        norm_url = clean_url(url)
        raw_url = url.strip()

        # Build candidate URL variations to account for trailing slashes / raw vs cleaned
        url_candidates = [
            norm_url.rstrip("/"),
            norm_url.rstrip("/") + "/",
            raw_url.rstrip("/"),
            raw_url.rstrip("/") + "/",
        ]

        secret = settings.secret_key.encode("utf-8")

        for u in url_candidates:
            # Check current payload format: {url}|{expiry}
            payload = f"{u}|{expiry}".encode("utf-8")
            expected_sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
            if hmac.compare_digest(provided_signature, expected_sig):
                return True

            # Check legacy payload formats for backward compatibility
            for legacy_fmt in ("best", format_id, "any"):
                legacy_payload = f"{u}|{legacy_fmt}|{item_id}|{expiry}".encode("utf-8")
                legacy_sig = hmac.new(secret, legacy_payload, hashlib.sha256).hexdigest()
                if hmac.compare_digest(provided_signature, legacy_sig):
                    return True

        return False

    except (ValueError, TypeError, Exception):
        return False
