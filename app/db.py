from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .config import DATABASE_PATH, DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME, DEFAULT_SETTINGS
from .security import hash_password, new_secret


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transcription_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                original_filename TEXT NOT NULL,
                media_path TEXT NOT NULL,
                transcript_path TEXT,
                language TEXT NOT NULL,
                model_path TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'upload',
                live_session_id TEXT,
                status TEXT NOT NULL,
                progress_percent INTEGER NOT NULL DEFAULT 0,
                progress_stage TEXT,
                media_duration_seconds REAL,
                processed_seconds REAL,
                transcript_text TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS live_sessions (
                id TEXT PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES transcription_jobs(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                language TEXT NOT NULL,
                model_path TEXT NOT NULL,
                final_media_path TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                stopped_at TEXT,
                finished_at TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS live_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES live_sessions(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                chunk_path TEXT NOT NULL,
                status TEXT NOT NULL,
                transcript_text TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                UNIQUE(session_id, sequence)
            );
            """
        )
        _ensure_column(conn, "transcription_jobs", "source_type", "TEXT NOT NULL DEFAULT 'upload'")
        _ensure_column(conn, "transcription_jobs", "live_session_id", "TEXT")
        _ensure_column(conn, "transcription_jobs", "progress_percent", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "transcription_jobs", "progress_stage", "TEXT")
        _ensure_column(conn, "transcription_jobs", "media_duration_seconds", "REAL")
        _ensure_column(conn, "transcription_jobs", "processed_seconds", "REAL")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('secret_key', ?)", (new_secret(),))
        existing_admin = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if not existing_admin:
            conn.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?, ?, 'admin')",
                (DEFAULT_ADMIN_USERNAME, hash_password(DEFAULT_ADMIN_PASSWORD)),
            )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def get_settings() -> dict[str, str]:
    with connect() as conn:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
