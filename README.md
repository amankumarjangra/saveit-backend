# SaveIt Backend

**Link se Video & Image Downloader** – Stateless FastAPI backend.

## Tech Stack
- **Python 3.11+** / FastAPI / Uvicorn
- **yt-dlp** (YouTube, Instagram video extraction)
- **gallery-dl** (Instagram image/carousel fallback)
- **FFmpeg** (adaptive stream merging — no re-encoding)
- In-memory TTL cache (`cachetools`) — no database

## Quick Start

### Prerequisites
- Python 3.11+
- FFmpeg installed and on PATH

### Local Development
```bash
# Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/macOS

# Install dependencies
pip install -r requirements.txt

# Run server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Docker
```bash
docker compose up --build
```

## API Endpoints
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/health` | Health check |
| POST | `/api/v1/resolve` | Extract metadata & formats |
| GET | `/api/v1/download` | Download media (merge/proxy) |

API docs available at `http://localhost:8000/docs` (debug mode only).

## Architecture
```
app/
├── __init__.py
├── main.py          # FastAPI app factory, CORS, lifespan
├── config.py        # pydantic-settings configuration
├── schemas.py       # Pydantic request/response models
├── routes.py        # API endpoint handlers
├── extractor.py     # yt-dlp / gallery-dl extraction + FFmpeg merge
├── url_utils.py     # URL validation, cleaning, SSRF protection
└── token_utils.py   # HMAC download token generation/verification
```
