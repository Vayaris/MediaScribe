from __future__ import annotations

import time

from .config import ensure_directories
from .db import connect, init_db
from .transcriber import run_transcription


def claim_next_job() -> int | None:
    with connect() as conn:
        row = conn.execute("SELECT id FROM transcription_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1").fetchone()
        return int(row["id"]) if row else None


def requeue_interrupted_jobs() -> None:
    with connect() as conn:
        conn.execute("UPDATE transcription_jobs SET status = 'queued', error = NULL WHERE status = 'running'")


def main() -> None:
    ensure_directories()
    init_db()
    requeue_interrupted_jobs()
    while True:
        job_id = claim_next_job()
        if job_id is None:
            time.sleep(2)
            continue
        run_transcription(job_id)


if __name__ == "__main__":
    main()
