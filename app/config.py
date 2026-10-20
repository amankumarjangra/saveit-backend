"""
SaveIt Backend - Configuration module.

Loads settings from environment variables / .env file using pydantic-settings.
No database configuration needed — the backend is fully stateless.
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    api_version: str = "1.0.0"

    # Security
    secret_key: str = "change-me-to-a-random-64-char-string"
    allowed_origins: str = "*"

    # Rate Limiting
    rate_limit: str = "30/minute"

    # Download Limits
    max_file_size_mb: int = 2048
    max_duration_seconds: int = 10800  # 3 hours
    max_concurrent_merges: int = 5

    # Cache
    metadata_cache_ttl_seconds: int = 600  # 10 minutes
    metadata_cache_max_size: int = 500

    # Token
    token_expiry_seconds: int = 300  # 5 minutes

    # Temp Files
    temp_dir: str = "/tmp/saveit"
    temp_cleanup_interval_seconds: int = 600  # 10 minutes
    temp_max_age_seconds: int = 900  # 15 minutes

    # FFmpeg
    ffmpeg_path: str = "ffmpeg"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    """Returns cached settings instance."""
    return Settings()
