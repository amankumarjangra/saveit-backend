"""
SaveIt Backend - Pydantic schemas for API request/response models.

Defines all data models used across the API endpoints. These models ensure
strict validation and clean serialization without any database dependency.
"""

from pydantic import BaseModel, Field, HttpUrl
from typing import Optional, List
from enum import Enum


# ──────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────

class Platform(str, Enum):
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    UNKNOWN = "unknown"


class ContentType(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    CAROUSEL = "carousel"
    AUDIO = "audio"


class DownloadMode(str, Enum):
    DIRECT = "direct"
    MERGE = "merge"


class ErrorCode(str, Enum):
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    CONTENT_PRIVATE = "CONTENT_PRIVATE"
    CONTENT_NOT_FOUND = "CONTENT_NOT_FOUND"
    AGE_RESTRICTED = "AGE_RESTRICTED"
    FETCH_TIMEOUT = "FETCH_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    SERVER_ERROR = "SERVER_ERROR"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    DURATION_TOO_LONG = "DURATION_TOO_LONG"
    MERGE_QUEUE_FULL = "MERGE_QUEUE_FULL"


# ──────────────────────────────────────────────
# Request Models
# ──────────────────────────────────────────────

class ResolveRequest(BaseModel):
    """Request body for POST /api/v1/resolve."""
    url: str = Field(
        ...,
        min_length=10,
        max_length=2048,
        description="The URL to resolve (Instagram or YouTube link).",
        examples=["https://www.instagram.com/reel/XXXX/"],
    )


# ──────────────────────────────────────────────
# Response Models
# ──────────────────────────────────────────────

class FormatInfo(BaseModel):
    """A single downloadable format option."""
    format_id: str
    label: str
    resolution: Optional[str] = None
    ext: str
    filesize: Optional[int] = None
    download_mode: DownloadMode
    direct_url: Optional[str] = None


class MediaItem(BaseModel):
    """A single media item (could be one video, one image, or one item in a carousel)."""
    item_id: str
    type: ContentType
    thumbnail: Optional[str] = None
    formats: List[FormatInfo]


class ResolveResponse(BaseModel):
    """Response body for POST /api/v1/resolve."""
    platform: Platform
    type: ContentType
    title: Optional[str] = None
    uploader: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    items: List[MediaItem]
    download_token: Optional[str] = None


class HealthResponse(BaseModel):
    """Response body for GET /api/v1/health."""
    status: str = "ok"
    version: str


class ErrorDetail(BaseModel):
    """Error detail object."""
    code: ErrorCode
    message: str


class ErrorResponse(BaseModel):
    """Standard error response."""
    error: ErrorDetail
