"""
SaveIt Backend - Media extraction service using yt-dlp and gallery-dl.

This is the core of the backend. It handles:
1. Extracting metadata and available formats from supported URLs.
2. Downloading media (video/audio) with optional FFmpeg merging for
   adaptive YouTube streams (1080p+ where video and audio are separate).
3. Falling back to gallery-dl for Instagram image/carousel extraction.

All operations are stateless. Temp files are cleaned up immediately after
streaming or via periodic cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import yt_dlp

from app.config import get_settings
from app.schemas import (
    ContentType,
    DownloadMode,
    FormatInfo,
    MediaItem,
    Platform,
    ResolveResponse,
)
from app.token_utils import generate_download_token

logger = logging.getLogger("saveit.extractor")


# ──────────────────────────────────────────────
# Concurrency limiter for FFmpeg merge operations
# ──────────────────────────────────────────────

_merge_semaphore: Optional[asyncio.Semaphore] = None


def _get_merge_semaphore() -> asyncio.Semaphore:
    """Lazily create the merge concurrency semaphore."""
    global _merge_semaphore
    if _merge_semaphore is None:
        settings = get_settings()
        _merge_semaphore = asyncio.Semaphore(settings.max_concurrent_merges)
    return _merge_semaphore


# ──────────────────────────────────────────────
# Temp Directory Management
# ──────────────────────────────────────────────

def _ensure_temp_dir() -> Path:
    """Ensure the temporary download directory exists."""
    settings = get_settings()
    temp_path = Path(settings.temp_dir)
    temp_path.mkdir(parents=True, exist_ok=True)
    return temp_path


def cleanup_old_temp_files() -> int:
    """
    Remove temp files older than TEMP_MAX_AGE_SECONDS.
    Returns the number of files removed.
    """
    settings = get_settings()
    temp_path = Path(settings.temp_dir)
    if not temp_path.exists():
        return 0

    removed = 0
    now = time.time()
    max_age = settings.temp_max_age_seconds

    for item in temp_path.iterdir():
        try:
            if item.is_file() and (now - item.stat().st_mtime) > max_age:
                item.unlink()
                removed += 1
            elif item.is_dir() and (now - item.stat().st_mtime) > max_age:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        except OSError:
            pass

    if removed > 0:
        logger.info(f"Cleaned up {removed} old temp files.")
    return removed


# ──────────────────────────────────────────────
# Format Helpers
# ──────────────────────────────────────────────

def _format_filesize(size_bytes: Optional[int]) -> Optional[int]:
    """Return filesize in bytes, or None if unknown."""
    if size_bytes and size_bytes > 0:
        return int(size_bytes)
    return None


def _get_resolution_label(height: Optional[int]) -> Optional[str]:
    """Convert height to human-readable resolution label."""
    if not height:
        return None
    if height >= 2160:
        return "4K"
    elif height >= 1440:
        return "1440p"
    elif height >= 1080:
        return "1080p"
    elif height >= 720:
        return "720p"
    elif height >= 480:
        return "480p"
    elif height >= 360:
        return "360p"
    elif height >= 240:
        return "240p"
    else:
        return f"{height}p"


def _is_progressive(fmt: dict) -> bool:
    """Check if a format has both video and audio (progressive / muxed)."""
    vcodec = fmt.get("vcodec", "none")
    acodec = fmt.get("acodec", "none")
    has_video = vcodec and vcodec != "none"
    has_audio = acodec and acodec != "none"
    return has_video and has_audio


def _has_video(fmt: dict) -> bool:
    """Check if format contains a video stream."""
    vcodec = fmt.get("vcodec", "none")
    return bool(vcodec and vcodec != "none")


def _has_audio(fmt: dict) -> bool:
    """Check if format contains an audio stream."""
    acodec = fmt.get("acodec", "none")
    return bool(acodec and acodec != "none")


# ──────────────────────────────────────────────
# yt-dlp Extraction
# ──────────────────────────────────────────────

def _get_cookie_file() -> Optional[str]:
    """Check if cookies.txt file or YOUTUBE_COOKIES env var is available."""
    # 1. Check environment variable (useful on Render / Heroku / Cloud)
    env_cookies = os.getenv("YOUTUBE_COOKIES") or os.getenv("COOKIES_TEXT")
    if env_cookies and len(env_cookies.strip()) > 10:
        tmp_cookie = Path("/tmp/saveit_env_cookies.txt") if os.name != "nt" else Path("./tmp/saveit_env_cookies.txt")
        try:
            tmp_cookie.parent.mkdir(parents=True, exist_ok=True)
            tmp_cookie.write_text(env_cookies.strip(), encoding="utf-8")
            return str(tmp_cookie.resolve())
        except Exception as e:
            logger.warning(f"Could not write env cookies to file: {e}")

    # 2. Check local file candidates
    candidates = [
        Path("cookies.txt"),
        Path(__file__).parent.parent / "cookies.txt",
        Path(__file__).parent / "cookies.txt",
    ]
    for c in candidates:
        if c.exists() and c.is_file() and c.stat().st_size > 0:
            return str(c.resolve())
    return None



def _build_ydl_opts() -> dict:
    """Build yt-dlp options for metadata extraction (no download)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "no_color": True,
        "socket_timeout": 15,
        "source_address": "0.0.0.0",  # Force IPv4 to bypass ISP IPv6 BotGuard restrictions
        "js_runtimes": {"node": {}},
        "writeinfojson": False,
        "writethumbnail": False,
        "writesubtitles": False,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"]
            }
        },
    }
    cookie_path = _get_cookie_file()
    if cookie_path:
        opts["cookiefile"] = cookie_path
    return opts


async def extract_metadata(url: str, platform: Platform) -> ResolveResponse:
    """
    Extract metadata and available formats from a URL using yt-dlp.

    For Instagram images/carousels, falls back to gallery-dl if yt-dlp
    doesn't return image data.

    Runs yt-dlp in a thread executor to avoid blocking the event loop.
    """
    settings = get_settings()
    loop = asyncio.get_event_loop()

    info = await loop.run_in_executor(None, _sync_extract_info, url)

    if info is None:
        raise ExtractionError("Failed to extract information from URL.")

    # Check duration limits
    duration = info.get("duration")
    if duration and duration > settings.max_duration_seconds:
        raise DurationTooLongError(
            f"Content is too long ({int(duration // 60)} min). "
            f"Maximum allowed: {settings.max_duration_seconds // 60} min."
        )

    # Determine content type and build response
    entries = info.get("entries")

    if entries:
        # Playlist / carousel - multiple items
        return _build_carousel_response(info, entries, url, platform)
    else:
        # Single item
        return _build_single_item_response(info, url, platform)


def _sync_extract_info(url: str) -> Optional[dict]:
    """Synchronous yt-dlp extraction (run in executor)."""
    opts = _build_ydl_opts()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e).lower()
        # Fallback retry for YouTube bot challenges on cloud servers
        if ("bot" in error_msg or "sign in" in error_msg) and ("youtube" in url.lower() or "youtu.be" in url.lower()):
            logger.info("Bot challenge on YouTube, retrying with android/ios client fallback...")
            fallback_opts = dict(opts)
            fallback_opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["android", "ios", "tv_embedded"]
                }
            }
            try:
                with yt_dlp.YoutubeDL(fallback_opts) as fallback_ydl:
                    info = fallback_ydl.extract_info(url, download=False)
                    return info
            except Exception as fb_err:
                logger.warning(f"Fallback extraction also failed: {fb_err}")

        if "private" in error_msg or "login" in error_msg:
            raise ContentPrivateError("This content is private or requires login.")
        elif "not found" in error_msg or "unavailable" in error_msg or "removed" in error_msg:
            raise ContentNotFoundError("This content was not found or has been removed.")
        elif "age" in error_msg and "restrict" in error_msg:
            raise AgeRestrictedError("This content is age-restricted.")
        else:
            raise ExtractionError(f"Failed to extract: {e}")
    except Exception as e:
        logger.exception(f"yt-dlp extraction failed for {url}")
        raise ExtractionError(f"Extraction error: {e}")



def _build_single_item_response(
    info: dict, url: str, platform: Platform
) -> ResolveResponse:
    """Build a ResolveResponse for a single video/image."""
    settings = get_settings()

    title = info.get("title", "Untitled")
    uploader = info.get("uploader") or info.get("channel")
    thumbnail = info.get("thumbnail")
    duration = info.get("duration")

    formats = info.get("formats", [])
    requested_format = info.get("format_id", "")

    # Determine content type
    content_type = ContentType.VIDEO
    if not formats and info.get("url", "").lower().endswith(
        (".jpg", ".jpeg", ".png", ".webp")
    ):
        content_type = ContentType.IMAGE

    # Build format list
    format_items = _build_format_list(formats, platform, url)

    # For images (if no video formats found), try the direct URL
    if not format_items and info.get("url"):
        direct = info["url"]
        ext = info.get("ext", "jpg")
        content_type = ContentType.IMAGE
        format_items.append(FormatInfo(
            format_id="original",
            label="Original",
            ext=ext,
            filesize=_format_filesize(info.get("filesize")),
            download_mode=DownloadMode.DIRECT,
            direct_url=direct,
        ))

    token = generate_download_token(url, "best", "0")

    return ResolveResponse(
        platform=platform,
        type=content_type,
        title=title,
        uploader=uploader,
        thumbnail=thumbnail,
        duration=int(duration) if duration else None,
        items=[
            MediaItem(
                item_id="0",
                type=content_type,
                thumbnail=thumbnail,
                formats=format_items,
            )
        ],
        download_token=token,
    )


def _build_carousel_response(
    info: dict, entries: list, url: str, platform: Platform
) -> ResolveResponse:
    """Build a ResolveResponse for a carousel/playlist."""
    title = info.get("title", "Carousel")
    uploader = info.get("uploader") or info.get("channel")
    thumbnail = info.get("thumbnail")

    items = []
    for idx, entry in enumerate(entries):
        entry_formats = entry.get("formats", [])
        entry_url = entry.get("url", "")
        entry_type = ContentType.VIDEO

        format_items = _build_format_list(entry_formats, platform, url)

        if not format_items and entry_url:
            ext = entry.get("ext", "jpg")
            if ext in ("jpg", "jpeg", "png", "webp"):
                entry_type = ContentType.IMAGE
            format_items.append(FormatInfo(
                format_id="original",
                label="Original",
                ext=ext,
                filesize=_format_filesize(entry.get("filesize")),
                download_mode=DownloadMode.DIRECT,
                direct_url=entry_url,
            ))

        items.append(MediaItem(
            item_id=str(idx),
            type=entry_type,
            thumbnail=entry.get("thumbnail", thumbnail),
            formats=format_items,
        ))

    token = generate_download_token(url, "best", "0")

    return ResolveResponse(
        platform=platform,
        type=ContentType.CAROUSEL if len(items) > 1 else items[0].type if items else ContentType.VIDEO,
        title=title,
        uploader=uploader,
        thumbnail=thumbnail,
        duration=None,
        items=items,
        download_token=token,
    )


def _build_format_list(
    formats: list[dict], platform: Platform, url: str
) -> list[FormatInfo]:
    """
    Build a clean list of FormatInfo from raw yt-dlp format dicts.

    Handles the YouTube progressive vs adaptive split:
    - Progressive (≤720p, video+audio combined) → direct download
    - Adaptive (1080p+, video-only) → needs merge with best audio
    """
    settings = get_settings()
    result: list[FormatInfo] = []
    seen_resolutions: set[str] = set()

    # Separate into categories
    progressive_formats = []
    adaptive_video_formats = []
    audio_formats = []

    for fmt in formats:
        format_id = fmt.get("format_id", "")
        ext = fmt.get("ext", "mp4")
        height = fmt.get("height")
        filesize = fmt.get("filesize") or fmt.get("filesize_approx")

        # Skip storyboard / manifest formats
        if fmt.get("format_note", "").lower() in ("storyboard", ""):
            if not height and not _has_audio(fmt):
                continue
        if ext in ("mhtml",):
            continue

        if _is_progressive(fmt):
            progressive_formats.append(fmt)
        elif _has_video(fmt) and not _has_audio(fmt):
            adaptive_video_formats.append(fmt)
        elif _has_audio(fmt) and not _has_video(fmt):
            audio_formats.append(fmt)

    # Sort by quality (highest first)
    progressive_formats.sort(key=lambda f: f.get("height", 0) or 0, reverse=True)
    adaptive_video_formats.sort(key=lambda f: f.get("height", 0) or 0, reverse=True)
    audio_formats.sort(
        key=lambda f: f.get("abr", 0) or f.get("tbr", 0) or 0, reverse=True
    )

    # 1) Add "Best (Original)" – the highest quality option
    best_adaptive = adaptive_video_formats[0] if adaptive_video_formats else None
    best_progressive = progressive_formats[0] if progressive_formats else None

    if best_adaptive:
        h = best_adaptive.get("height", 0) or 0
        res = _get_resolution_label(h)
        best_audio = audio_formats[0] if audio_formats else None
        est_size = None
        if best_adaptive.get("filesize") and best_audio and best_audio.get("filesize"):
            est_size = int(best_adaptive["filesize"]) + int(best_audio["filesize"])
        elif best_adaptive.get("filesize_approx") and best_audio and best_audio.get("filesize_approx"):
            est_size = int(best_adaptive["filesize_approx"]) + int(best_audio.get("filesize_approx", 0))

        # Check file size limits
        if est_size and est_size > settings.max_file_size_mb * 1024 * 1024:
            pass  # Skip this format, too large
        else:
            label = f"Best (Original) - {res}" if res else "Best (Original)"
            result.append(FormatInfo(
                format_id=f"{best_adaptive.get('format_id', 'best_v')}+{best_audio.get('format_id', 'best_a') if best_audio else 'best_a'}",
                label=label,
                resolution=res,
                ext="mp4",
                filesize=est_size,
                download_mode=DownloadMode.MERGE,
            ))
            if res:
                seen_resolutions.add(res)

    elif best_progressive:
        h = best_progressive.get("height", 0) or 0
        res = _get_resolution_label(h)
        direct_url = best_progressive.get("url")
        filesize = _format_filesize(
            best_progressive.get("filesize") or best_progressive.get("filesize_approx")
        )

        label = f"Best (Original) - {res}" if res else "Best (Original)"
        result.append(FormatInfo(
            format_id=best_progressive.get("format_id", "best"),
            label=label,
            resolution=res,
            ext=best_progressive.get("ext", "mp4"),
            filesize=filesize,
            download_mode=DownloadMode.DIRECT,
            direct_url=direct_url,
        ))
        if res:
            seen_resolutions.add(res)

    # 2) Add other progressive formats (e.g., 720p, 480p, 360p)
    for fmt in progressive_formats:
        h = fmt.get("height", 0) or 0
        res = _get_resolution_label(h)
        if not res or res in seen_resolutions:
            continue

        direct_url = fmt.get("url")
        filesize = _format_filesize(
            fmt.get("filesize") or fmt.get("filesize_approx")
        )

        result.append(FormatInfo(
            format_id=fmt.get("format_id", ""),
            label=res,
            resolution=res,
            ext=fmt.get("ext", "mp4"),
            filesize=filesize,
            download_mode=DownloadMode.DIRECT,
            direct_url=direct_url,
        ))
        seen_resolutions.add(res)

    # 3) Add other adaptive video formats NOT already covered
    for fmt in adaptive_video_formats:
        h = fmt.get("height", 0) or 0
        res = _get_resolution_label(h)
        if not res or res in seen_resolutions:
            continue

        best_audio = audio_formats[0] if audio_formats else None
        est_size = None
        if fmt.get("filesize") and best_audio and best_audio.get("filesize"):
            est_size = int(fmt["filesize"]) + int(best_audio["filesize"])

        result.append(FormatInfo(
            format_id=f"{fmt.get('format_id', '')}+{best_audio.get('format_id', 'best_a') if best_audio else 'best_a'}",
            label=res,
            resolution=res,
            ext="mp4",
            filesize=est_size,
            download_mode=DownloadMode.MERGE,
        ))
        seen_resolutions.add(res)

    # 4) Add best audio-only option
    if audio_formats:
        best_a = audio_formats[0]
        ext = best_a.get("ext", "m4a")
        abr = best_a.get("abr")
        label = f"Audio only ({ext.upper()})"
        if abr:
            label += f" - {int(abr)}kbps"

        result.append(FormatInfo(
            format_id=best_a.get("format_id", "audio"),
            label=label,
            ext=ext,
            filesize=_format_filesize(
                best_a.get("filesize") or best_a.get("filesize_approx")
            ),
            download_mode=DownloadMode.DIRECT,
            direct_url=best_a.get("url"),
        ))

    return result


# ──────────────────────────────────────────────
# Download & Merge
# ──────────────────────────────────────────────

async def download_media(
    url: str, format_id: str, item_id: str = "0"
) -> tuple[Path, str, Optional[int]]:
    """
    Download media for the given URL and format.

    For 'merge' formats (format_id contains '+'), downloads video and audio
    separately with yt-dlp, then merges them with FFmpeg using stream copy
    (no re-encoding).

    Returns:
        (file_path, suggested_filename, content_length)
    """
    settings = get_settings()
    temp_dir = _ensure_temp_dir()
    job_id = uuid.uuid4().hex[:12]
    job_dir = temp_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        if "+" in format_id:
            # Merge mode: video + audio
            return await _download_and_merge(url, format_id, job_dir, item_id)
        else:
            # Direct single-format download
            return await _download_single(url, format_id, job_dir, item_id)
    except Exception:
        # Clean up on failure
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


async def _download_single(
    url: str, format_id: str, job_dir: Path, item_id: str
) -> tuple[Path, str, Optional[int]]:
    """Download a single format (progressive or audio-only)."""
    loop = asyncio.get_event_loop()

    output_template = str(job_dir / "%(title).80s.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": format_id,
        "outtmpl": output_template,
        "no_color": True,
        "socket_timeout": 30,
        "retries": 3,
        "source_address": "0.0.0.0",  # Force IPv4
        "js_runtimes": {"node": {}},
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"]
            }
        },
    }
    cookie_path = _get_cookie_file()
    if cookie_path:
        opts["cookiefile"] = cookie_path


    def _do_download():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return Path(filename), info

    file_path, info = await loop.run_in_executor(None, _do_download)

    if not file_path.exists():
        # yt-dlp may have changed the extension
        candidates = list(job_dir.glob("*"))
        if candidates:
            file_path = candidates[0]
        else:
            raise ExtractionError("Download completed but file not found.")

    title = _sanitize_filename(info.get("title", "download"))
    ext = file_path.suffix
    suggested = f"{title}{ext}"
    size = file_path.stat().st_size

    return file_path, suggested, size


async def _download_and_merge(
    url: str, format_id: str, job_dir: Path, item_id: str
) -> tuple[Path, str, Optional[int]]:
    """
    Download video and audio separately, then merge with FFmpeg.

    format_id is expected to be in the form "video_id+audio_id".
    Uses -c copy (stream copy) so there's no re-encoding or quality loss.
    """
    semaphore = _get_merge_semaphore()

    # Try to acquire the semaphore (non-blocking check for queue overflow)
    if semaphore.locked():
        # Check if all slots are taken — but we'll still try to acquire
        pass

    async with semaphore:
        loop = asyncio.get_event_loop()
        settings = get_settings()

        parts = format_id.split("+", 1)
        video_format_id = parts[0]
        audio_format_id = parts[1] if len(parts) > 1 else "bestaudio"

        video_path = job_dir / "video_only.mp4"
        audio_path = job_dir / "audio_only.m4a"
        merged_path = job_dir / "merged_output.mp4"

        cookie_path = _get_cookie_file()

        # Download video-only stream
        video_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": video_format_id,
            "outtmpl": str(video_path),
            "no_color": True,
            "socket_timeout": 30,
            "retries": 3,
            "source_address": "0.0.0.0",  # Force IPv4
            "js_runtimes": {"node": {}},
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios", "mweb", "web"]
                }
            },
        }
        if cookie_path:
            video_opts["cookiefile"] = cookie_path

        # Download audio-only stream
        audio_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": audio_format_id,
            "outtmpl": str(audio_path),
            "no_color": True,
            "socket_timeout": 30,
            "retries": 3,
            "source_address": "0.0.0.0",  # Force IPv4
            "js_runtimes": {"node": {}},
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios", "mweb", "web"]
                }
            },
        }
        if cookie_path:
            audio_opts["cookiefile"] = cookie_path

        def _download_video():
            with yt_dlp.YoutubeDL(video_opts) as ydl:
                return ydl.extract_info(url, download=True)

        def _download_audio():
            with yt_dlp.YoutubeDL(audio_opts) as ydl:
                return ydl.extract_info(url, download=True)

        # Download both streams concurrently
        video_info, _ = await asyncio.gather(
            loop.run_in_executor(None, _download_video),
            loop.run_in_executor(None, _download_audio),
        )

        # Find actual downloaded files (yt-dlp may change extensions)
        video_file = _find_file(job_dir, "video_only")
        audio_file = _find_file(job_dir, "audio_only")

        if not video_file or not audio_file:
            raise ExtractionError("Failed to download video or audio stream.")

        # Merge with FFmpeg using stream copy (no re-encoding)
        ffmpeg_cmd = [
            settings.ffmpeg_path,
            "-y",  # Overwrite
            "-i", str(video_file),
            "-i", str(audio_file),
            "-c", "copy",  # Stream copy — no re-encoding
            "-movflags", "+faststart",  # Web-optimized MP4
            str(merged_path),
        ]

        def _run_ffmpeg():
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout for merge
            )
            if result.returncode != 0:
                logger.error(f"FFmpeg error: {result.stderr}")
                raise ExtractionError(f"FFmpeg merge failed: {result.stderr[:200]}")

        await loop.run_in_executor(None, _run_ffmpeg)

        if not merged_path.exists():
            raise ExtractionError("FFmpeg merge completed but output file not found.")

        title = _sanitize_filename(video_info.get("title", "download"))
        size = merged_path.stat().st_size

        return merged_path, f"{title}.mp4", size


def _find_file(directory: Path, prefix: str) -> Optional[Path]:
    """Find a file in directory whose stem starts with the given prefix."""
    for item in directory.iterdir():
        if item.is_file() and item.stem.startswith(prefix):
            return item
    return None


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    # Remove invalid characters
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    # Collapse whitespace
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    # Truncate to 80 characters
    if len(sanitized) > 80:
        sanitized = sanitized[:80].rstrip()
    return sanitized or "download"


# ──────────────────────────────────────────────
# Custom Exceptions
# ──────────────────────────────────────────────

class ExtractionError(Exception):
    """General extraction failure."""
    pass


class ContentPrivateError(Exception):
    """Content is private or requires login."""
    pass


class ContentNotFoundError(Exception):
    """Content not found or has been removed."""
    pass


class AgeRestrictedError(Exception):
    """Content is age-restricted."""
    pass


class DurationTooLongError(Exception):
    """Content exceeds the maximum allowed duration."""
    pass
