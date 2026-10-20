# ──────────────────────────────────────────────
# SaveIt Backend – Docker Configuration
# ──────────────────────────────────────────────
# Multi-stage build for a lean production image.
# Includes yt-dlp, gallery-dl, and FFmpeg.

FROM python:3.11-slim AS base

# Install FFmpeg, Node.js (for yt-dlp challenge execution), and clean up apt cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg nodejs && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create temp directory
RUN mkdir -p /tmp/saveit

# Expose default port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.getenv('PORT', '8000'); urllib.request.urlopen(f'http://localhost:{port}/api/v1/health')" || exit 1

# Run with uvicorn (Render dynamically sets $PORT)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]

