from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import TRANSCRIPT_DIR
from .db import connect, get_setting


def _mark(job_id: int, status: str, *, transcript_text: str | None = None, transcript_path: str | None = None, error: str | None = None) -> None:
    with connect() as conn:
        if status == "running":
            conn.execute("UPDATE transcription_jobs SET status = ?, started_at = CURRENT_TIMESTAMP, error = NULL WHERE id = ?", (status, job_id))
        elif status in {"completed", "failed"}:
            conn.execute(
                "UPDATE transcription_jobs SET status = ?, transcript_text = ?, transcript_path = ?, error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, transcript_text, transcript_path, error, job_id),
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
    whisper_binary = Path(get_setting("whisper_binary"))

    try:
        if not media_path.exists():
            raise RuntimeError("Uploaded media file is missing.")
        if not model_path.exists():
            raise RuntimeError(f"Whisper model not found: {model_path}")
        if not whisper_binary.exists():
            raise RuntimeError(f"whisper.cpp binary not found: {whisper_binary}")
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is not installed or not available in PATH.")

        transcript_path = TRANSCRIPT_DIR / f"job-{job_id}.txt"

        with tempfile.TemporaryDirectory(prefix="mediascribe-") as tmp:
            wav_path = Path(tmp) / "audio.wav"
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(media_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(wav_path),
            ]
            subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            output_prefix = Path(tmp) / "transcript"
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
            ]
            subprocess.run(whisper_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            generated = output_prefix.with_suffix(".txt")
            if not generated.exists():
                raise RuntimeError("whisper.cpp did not produce a transcript file.")

            text = generated.read_text(encoding="utf-8", errors="replace").strip()
            transcript_path.write_text(text + "\n", encoding="utf-8")

        _mark(job_id, "completed", transcript_text=text, transcript_path=str(transcript_path))
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        _mark(job_id, "failed", error=detail[-4000:])
    except Exception as exc:
        _mark(job_id, "failed", error=str(exc))

