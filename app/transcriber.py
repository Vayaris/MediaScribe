from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import TRANSCRIPT_DIR
from .db import connect, get_setting


def _progress(job_id: int | None, percent: int, stage: str, *, processed: float | None = None, duration: float | None = None) -> None:
    if job_id is None:
        return
    percent = max(0, min(100, int(percent)))
    with connect() as conn:
        conn.execute(
            """
            UPDATE transcription_jobs
            SET progress_percent = ?, progress_stage = ?, processed_seconds = COALESCE(?, processed_seconds),
                media_duration_seconds = COALESCE(?, media_duration_seconds)
            WHERE id = ?
            """,
            (percent, stage, processed, duration, job_id),
        )


def _media_duration(media_path: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _convert_to_wav(media_path: Path, wav_path: Path, *, job_id: int | None = None, duration: float | None = None) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-progress",
        "pipe:1",
        "-nostats",
        str(wav_path),
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdout is not None
    for line in process.stdout:
        key, _, value = line.strip().partition("=")
        if key == "out_time_ms" and duration:
            try:
                processed = max(0.0, int(value) / 1_000_000)
            except ValueError:
                continue
            _progress(job_id, 5 + int(min(processed / duration, 1.0) * 25), "Conversion audio", processed=processed, duration=duration)
    stderr = process.stderr.read() if process.stderr else ""
    if process.wait() != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd, stderr=stderr)
    _progress(job_id, 30, "Audio préparé", duration=duration)


def _run_whisper(wav_path: Path, model_path: Path, language: str, output_prefix: Path, *, job_id: int | None = None) -> None:
    whisper_binary = Path(get_setting("whisper_binary"))
    whisper_cmd = [
        str(whisper_binary),
        "-m",
        str(model_path),
        "-f",
        str(wav_path),
        "-l",
        language,
        "-otxt",
        "-of",
        str(output_prefix),
        "-pp",
    ]
    process = subprocess.Popen(whisper_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None
    progress_re = re.compile(r"(\d{1,3})%")
    collected: list[str] = []
    for line in process.stdout:
        collected.append(line)
        match = progress_re.search(line)
        if match:
            whisper_percent = max(0, min(100, int(match.group(1))))
            _progress(job_id, 30 + int(whisper_percent * 0.7), "Transcription")
    if process.wait() != 0:
        raise subprocess.CalledProcessError(process.returncode, whisper_cmd, output="".join(collected))


def transcribe_media(media_path: Path, model_path: Path, language: str, output_name: str, *, persist: bool = True, job_id: int | None = None) -> str:
    whisper_binary = Path(get_setting("whisper_binary"))
    if not media_path.exists():
        raise RuntimeError("Media file is missing.")
    if not model_path.exists():
        raise RuntimeError(f"Whisper model not found: {model_path}")
    if not whisper_binary.exists():
        raise RuntimeError(f"whisper.cpp binary not found: {whisper_binary}")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed or not available in PATH.")

    transcript_path = TRANSCRIPT_DIR / output_name
    duration = _media_duration(media_path)
    _progress(job_id, 2, "Analyse du média", duration=duration)

    with tempfile.TemporaryDirectory(prefix="mediascribe-") as tmp:
        wav_path = Path(tmp) / "audio.wav"
        _convert_to_wav(media_path, wav_path, job_id=job_id, duration=duration)

        output_prefix = Path(tmp) / "transcript"
        _progress(job_id, 32, "Transcription")
        _run_whisper(wav_path, model_path, language, output_prefix, job_id=job_id)
        generated = output_prefix.with_suffix(".txt")
        if not generated.exists():
            raise RuntimeError("whisper.cpp did not produce a transcript file.")

        text = generated.read_text(encoding="utf-8", errors="replace").strip()
        if persist:
            transcript_path.write_text(text + "\n", encoding="utf-8")
        return text


def _mark(job_id: int, status: str, *, transcript_text: str | None = None, transcript_path: str | None = None, error: str | None = None) -> None:
    with connect() as conn:
        if status == "running":
            conn.execute(
                "UPDATE transcription_jobs SET status = ?, started_at = CURRENT_TIMESTAMP, error = NULL, progress_percent = 1, progress_stage = 'En file de traitement' WHERE id = ?",
                (status, job_id),
            )
        elif status in {"completed", "failed"}:
            percent = 100 if status == "completed" else None
            stage = "Terminé" if status == "completed" else "Échec"
            conn.execute(
                """
                UPDATE transcription_jobs
                SET status = ?, transcript_text = ?, transcript_path = ?, error = ?, finished_at = CURRENT_TIMESTAMP,
                    progress_percent = COALESCE(?, progress_percent), progress_stage = ?
                WHERE id = ?
                """,
                (status, transcript_text, transcript_path, error, percent, stage, job_id),
            )
        else:
            conn.execute("UPDATE transcription_jobs SET status = ? WHERE id = ?", (status, job_id))


def run_transcription(job_id: int) -> None:
    with connect() as conn:
        job = conn.execute("SELECT * FROM transcription_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        return

    _mark(job_id, "running")

    media_path = Path(job["media_path"])
    model_path = Path(job["model_path"])
    language = job["language"]

    try:
        transcript_path = TRANSCRIPT_DIR / f"job-{job_id}.txt"
        text = transcribe_media(media_path, model_path, language, f"job-{job_id}.txt", job_id=job_id)
        _mark(job_id, "completed", transcript_text=text, transcript_path=str(transcript_path))
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        _mark(job_id, "failed", error=detail[-4000:])
    except Exception as exc:
        _mark(job_id, "failed", error=str(exc))


def run_live_chunk(chunk_id: int) -> None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT c.*, s.job_id, s.language, s.model_path
            FROM live_chunks c
            JOIN live_sessions s ON s.id = c.session_id
            WHERE c.id = ?
            """,
            (chunk_id,),
        ).fetchone()
    if not row:
        return

    with connect() as conn:
        conn.execute("UPDATE live_chunks SET status = 'running', started_at = CURRENT_TIMESTAMP, error = NULL WHERE id = ?", (chunk_id,))

    try:
        text = transcribe_media(
            Path(row["chunk_path"]),
            Path(row["model_path"]),
            row["language"],
            f"live-{row['session_id']}-{row['sequence']}.txt",
            persist=False,
        )
        with connect() as conn:
            job = conn.execute("SELECT transcript_text FROM transcription_jobs WHERE id = ?", (row["job_id"],)).fetchone()
            current = (job["transcript_text"] or "").strip() if job else ""
            combined = "\n\n".join(part for part in [current, text] if part).strip()
            transcript_path = TRANSCRIPT_DIR / f"job-{row['job_id']}.txt"
            transcript_path.write_text((combined + "\n") if combined else "", encoding="utf-8")
            conn.execute(
                """
                UPDATE live_chunks
                SET status = 'completed', transcript_text = ?, finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (text, chunk_id),
            )
            conn.execute(
                "UPDATE transcription_jobs SET transcript_text = ?, transcript_path = ?, status = 'running' WHERE id = ?",
                (combined, str(transcript_path), row["job_id"]),
            )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        _mark_live_chunk_failed(chunk_id, row["job_id"], detail[-4000:])
    except Exception as exc:
        _mark_live_chunk_failed(chunk_id, row["job_id"], str(exc))


def _mark_live_chunk_failed(chunk_id: int, job_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE live_chunks
            SET status = 'failed', error = ?, finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error, chunk_id),
        )
        conn.execute("UPDATE transcription_jobs SET error = ?, progress_stage = 'Erreur sur un segment live' WHERE id = ?", (error, job_id))
