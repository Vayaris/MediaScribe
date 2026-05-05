from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "MediaScribe"
DEFAULT_ADMIN_USERNAME = os.getenv("MEDIASCRIBE_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("MEDIASCRIBE_ADMIN_PASSWORD", "ChangeMeNow!")

DATA_DIR = Path(os.getenv("MEDIASCRIBE_DATA_DIR", "/var/lib/mediascribe"))
UPLOAD_DIR = Path(os.getenv("MEDIASCRIBE_UPLOAD_DIR", str(DATA_DIR / "uploads")))
TRANSCRIPT_DIR = Path(os.getenv("MEDIASCRIBE_TRANSCRIPT_DIR", str(DATA_DIR / "transcripts")))
LIVE_DIR = Path(os.getenv("MEDIASCRIBE_LIVE_DIR", str(DATA_DIR / "live")))
EXPORT_DIR = Path(os.getenv("MEDIASCRIBE_EXPORT_DIR", str(DATA_DIR / "exports")))
MODEL_DIR = Path(os.getenv("MEDIASCRIBE_MODEL_DIR", "/opt/mediascribe/models"))
DATABASE_PATH = Path(os.getenv("MEDIASCRIBE_DB", str(DATA_DIR / "mediascribe.db")))

DEFAULT_SETTINGS = {
    "default_language": "fr",
    "model_path": str(MODEL_DIR / "ggml-small.bin"),
    "live_model_path": str(MODEL_DIR / "ggml-small.bin"),
    "live_chunk_seconds": "4",
    "whisper_binary": "/opt/mediascribe/whisper.cpp/build/bin/whisper-cli",
    "max_upload_mb": "2048",
    "max_concurrent_jobs": "1",
    "keep_uploaded_media": "true",
}

ALLOWED_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".mp3",
    ".flac",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
}


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
