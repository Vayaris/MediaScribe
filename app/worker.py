from __future__ import annotations

import subprocess
import time
import tempfile
from pathlib import Path

from .config import ensure_directories
from .db import connect, init_db
from .transcriber import run_live_chunk, run_transcription


def claim_next_job() -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM transcription_jobs WHERE status = 'queued' AND source_type = 'upload' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return int(row["id"]) if row else None


def claim_next_live_chunk() -> int | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT c.id
            FROM live_chunks c
            JOIN live_sessions s ON s.id = c.session_id
            WHERE c.status = 'queued' AND s.status IN ('recording', 'stopping')
            ORDER BY c.created_at ASC, c.sequence ASC
            LIMIT 1
            """
        ).fetchone()
        return int(row["id"]) if row else None


def requeue_interrupted_jobs() -> None:
    with connect() as conn:
        conn.execute("UPDATE transcription_jobs SET status = 'queued', error = NULL WHERE status = 'running' AND source_type = 'upload'")
        conn.execute("UPDATE live_chunks SET status = 'queued', error = NULL WHERE status = 'running'")


def finalize_stopped_live_sessions() -> bool:
    did_work = False
    with connect() as conn:
        sessions = conn.execute(
            """
            SELECT s.*
            FROM live_sessions s
            WHERE s.status = 'stopping'
              AND NOT EXISTS (
                SELECT 1 FROM live_chunks c
                WHERE c.session_id = s.id AND c.status IN ('queued', 'running')
              )
            ORDER BY s.stopped_at ASC
            """
        ).fetchall()

    for session in sessions:
        did_work = True
        with connect() as conn:
            chunks = conn.execute(
                "SELECT * FROM live_chunks WHERE session_id = ? ORDER BY sequence ASC",
                (session["id"],),
            ).fetchall()
            completed = [chunk for chunk in chunks if chunk["status"] == "completed"]
            failed = [chunk for chunk in chunks if chunk["status"] == "failed"]
            transcript_row = conn.execute(
                "SELECT transcript_text, transcript_path FROM transcription_jobs WHERE id = ?",
                (session["job_id"],),
            ).fetchone()

        if failed and not completed:
            error = failed[0]["error"] or "Un morceau live n'a pas pu être transcrit."
            with connect() as conn:
                conn.execute(
                    "UPDATE live_sessions SET status = 'failed', error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (error, session["id"]),
                )
                conn.execute(
                    "UPDATE transcription_jobs SET status = 'failed', error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (error, session["job_id"]),
                )
            continue

        final_path = Path(session["final_media_path"])
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if completed:
            try:
                _concat_live_chunks([Path(chunk["chunk_path"]) for chunk in completed], final_path)
            except Exception:
                with final_path.open("wb") as final_file:
                    for chunk in completed:
                        path = Path(chunk["chunk_path"])
                        if path.exists():
                            final_file.write(path.read_bytes())

        transcript_text = (transcript_row["transcript_text"] or "").strip() if transcript_row else ""
        transcript_path = transcript_row["transcript_path"] if transcript_row else None
        warning = f"{len(failed)} segment(s) live non transcrit(s)." if failed else None
        with connect() as conn:
            conn.execute(
                "UPDATE live_sessions SET status = 'completed', error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (warning, session["id"]),
            )
            conn.execute(
                """
                UPDATE transcription_jobs
                SET status = 'completed', transcript_text = ?, transcript_path = ?, media_path = ?, error = ?,
                    progress_percent = 100, progress_stage = 'Terminé', finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (transcript_text, transcript_path, str(final_path), warning, session["job_id"]),
            )
    return did_work


def _concat_live_chunks(paths: list[Path], final_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mediascribe-live-") as tmp:
        list_path = Path(tmp) / "chunks.txt"
        list_path.write_text("".join(f"file {str(path)!r}\n" for path in paths if path.exists()), encoding="utf-8")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libopus",
                str(final_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def main() -> None:
    ensure_directories()
    init_db()
    requeue_interrupted_jobs()
    while True:
        job_id = claim_next_job()
        if job_id is not None:
            run_transcription(job_id)
            continue

        chunk_id = claim_next_live_chunk()
        if chunk_id is not None:
            run_live_chunk(chunk_id)
            continue

        if finalize_stopped_live_sessions():
            continue

        if job_id is None:
            time.sleep(0.5)
            continue


if __name__ == "__main__":
    main()
