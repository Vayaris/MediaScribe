from __future__ import annotations

import html
import os
import shutil
import subprocess
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Cookie, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import ALLOWED_EXTENSIONS, APP_NAME, EXPORT_DIR, LIVE_DIR, MODEL_DIR, UPLOAD_DIR, ensure_directories
from .db import connect, get_setting, get_settings, init_db, set_setting
from .security import hash_password, sign_session, verify_password, verify_session


app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

PARIS_TZ = ZoneInfo("Europe/Paris")
LANGUAGE_OPTIONS = [
    ("fr", "Français"),
    ("en", "Anglais"),
    ("es", "Espagnol"),
    ("de", "Allemand"),
    ("it", "Italien"),
    ("pt", "Portugais"),
    ("auto", "Détection automatique"),
]
MODEL_CATALOG = {
    "tiny": {
        "label": "Tiny",
        "filename": "ggml-tiny.bin",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin",
        "min_ram_gb": 1,
        "rec_ram_gb": 2,
        "min_cpu": 1,
        "rec_cpu": 2,
    },
    "base": {
        "label": "Base",
        "filename": "ggml-base.bin",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
        "min_ram_gb": 1,
        "rec_ram_gb": 2,
        "min_cpu": 2,
        "rec_cpu": 2,
    },
    "small": {
        "label": "Small",
        "filename": "ggml-small.bin",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin",
        "min_ram_gb": 2,
        "rec_ram_gb": 4,
        "min_cpu": 2,
        "rec_cpu": 2,
    },
    "medium": {
        "label": "Medium",
        "filename": "ggml-medium.bin",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin",
        "min_ram_gb": 6,
        "rec_ram_gb": 8,
        "min_cpu": 4,
        "rec_cpu": 4,
    },
}


@app.on_event("startup")
def startup() -> None:
    ensure_directories()
    init_db()


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def status_class(status: str) -> str:
    normalized = (status or "").lower()
    return normalized if normalized in {"queued", "running", "completed", "failed"} else ""


def status_label(status: str) -> str:
    labels = {
        "queued": "En attente",
        "running": "En cours",
        "completed": "Terminé",
        "failed": "Échec",
    }
    return labels.get(status, status)


def language_label(code: str) -> str:
    return dict(LANGUAGE_OPTIONS).get(code, code)


def format_datetime(value: object) -> str:
    if not value:
        return ""
    raw = str(value)
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return raw
    return parsed.astimezone(PARIS_TZ).strftime("%d/%m/%y %H:%M:%S")


def get_user_job_number(conn, user_id: int, job_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS job_number
        FROM transcription_jobs
        WHERE user_id = ? AND id <= ?
        """,
        (user_id, job_id),
    ).fetchone()
    return int(row["job_number"] if row else 1)


def human_size(size: int | float | None) -> str:
    if size is None:
        return "-"
    return f"{size / (1024 ** 3):.1f} Go"


def language_options_html(current: str) -> str:
    return "".join(
        f"<option value='{code}' {'selected' if code == current else ''}>{label}</option>"
        for code, label in LANGUAGE_OPTIONS
    )


def available_models(current_model: str = "") -> list[Path]:
    models = [path for path in MODEL_DIR.iterdir() if path.is_file() and path.suffix.lower() in {".bin", ".gguf"}]
    if current_model:
        current = Path(current_model)
        if current.exists() and current.suffix.lower() in {".bin", ".gguf"} and current not in models:
            models.append(current)
    return sorted(models, key=lambda path: path.name.lower())


def model_options_html(current_model: str) -> str:
    models = available_models(current_model)
    if not models:
        return "<option value=''>Aucun modèle trouvé</option>"
    options = []
    current_resolved = str(Path(current_model).resolve()) if current_model else ""
    for path in models:
        selected = "selected" if str(path.resolve()) == current_resolved else ""
        profile = model_profile_for_path(path)
        suffix = f" - recommandé {profile['rec_ram_gb']} Go / {profile['rec_cpu']} coeurs" if profile else ""
        label = f"{path.name} - {human_size(path.stat().st_size)}{suffix}"
        options.append(f"<option value='{esc(path)}' {selected}>{esc(label)}</option>")
    return "".join(options)


def machine_resources() -> dict[str, int | None]:
    meminfo: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0]) * 1024
    except Exception:
        pass

    total_memory = meminfo.get("MemTotal")
    available_memory = meminfo.get("MemAvailable")
    disk = shutil.disk_usage("/")
    return {
        "cpu": os.cpu_count() or 1,
        "ram_total": total_memory,
        "ram_available": available_memory,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_free": disk.free,
    }


def machine_stats() -> dict[str, str]:
    resources = machine_resources()
    total_memory = resources["ram_total"]
    available_memory = resources["ram_available"]
    used_memory = total_memory - available_memory if total_memory is not None and available_memory is not None else None
    return {
        "cpu": str(resources["cpu"] or 1),
        "ram_total": human_size(total_memory),
        "ram_used": human_size(used_memory),
        "ram_available": human_size(available_memory),
        "disk_total": human_size(resources["disk_total"]),
        "disk_used": human_size(resources["disk_used"]),
        "disk_free": human_size(resources["disk_free"]),
    }


def model_profile_for_path(path: Path) -> dict | None:
    name = path.name.lower()
    if "tiny" in name:
        return MODEL_CATALOG["tiny"]
    if "base" in name:
        return MODEL_CATALOG["base"]
    if "medium" in name:
        return MODEL_CATALOG["medium"]
    if "small" in name:
        return MODEL_CATALOG["small"]
    return None


def model_resource_state(profile: dict | None) -> tuple[str, str]:
    if not profile:
        return "ok", "Modèle personnalisé"
    resources = machine_resources()
    ram_total = resources["ram_total"] or 0
    cpu = int(resources["cpu"] or 1)
    ram_gb = ram_total / (1024 ** 3)
    if ram_gb < profile["min_ram_gb"] or cpu < profile["min_cpu"]:
        return "danger", "Ressources insuffisantes"
    if ram_gb < profile["rec_ram_gb"] or cpu < profile["rec_cpu"]:
        return "warning", "Sous le recommandé"
    return "ok", "Machine compatible"


def model_catalog_html(current_model: str) -> str:
    installed = {path.name: path for path in available_models(current_model)}
    current_resolved = str(Path(current_model).resolve()) if current_model else ""
    cards = []
    for key, profile in MODEL_CATALOG.items():
        path = installed.get(profile["filename"], MODEL_DIR / profile["filename"])
        exists = path.exists()
        selected = exists and str(path.resolve()) == current_resolved
        state, state_label = model_resource_state(profile)
        action = ""
        if exists:
            action = f"""
              <form method="post" action="/admin/models/activate">
                <input type="hidden" name="model_path" value="{esc(path)}">
                <button class="secondary compact" type="submit" {'disabled' if selected else ''}>{'Actif' if selected else 'Activer'}</button>
              </form>"""
        else:
            action = f"""
              <form method="post" action="/admin/models/download">
                <input type="hidden" name="model_key" value="{esc(key)}">
                <button class="secondary compact" type="submit">Télécharger</button>
              </form>"""
        cards.append(
            f"""
            <div class="model-card {state}">
              <div>
                <strong>{esc(profile['label'])}</strong>
                <span>{'Installé' if exists else 'Disponible'} - min {profile['min_ram_gb']} Go / {profile['min_cpu']} coeurs, recommandé {profile['rec_ram_gb']} Go / {profile['rec_cpu']} coeurs</span>
                <small>{esc(state_label)}</small>
              </div>
              {action}
            </div>"""
        )
    return "".join(cards)


def nav_link(path: str, label: str, active: str, key: str) -> str:
    selected = " active" if active == key else ""
    return f"<a class='nav-link{selected}' href='{path}'>{label}</a>"


def base_document(title: str, body: str, *, user: dict | None = None, active: str = "", auth: bool = False) -> HTMLResponse:
    nav = ""
    if user:
        if user["role"] == "admin":
            links = nav_link("/admin", "Admin", active, "admin")
        else:
            links = f"""
              {nav_link("/", "Transcrire", active, "transcribe")}
              {nav_link("/jobs", "Historique", active, "jobs")}
              {nav_link("/account", "Compte", active, "account")}
            """
        nav = f"""
          <nav class="nav" aria-label="Navigation principale">
            {links}
            <button class="theme-toggle icon-only" type="button" onclick="toggleTheme()" aria-label="Changer de thème" title="Changer de thème"><span data-theme-icon>☾</span></button>
            <form class="logout-form" method="post" action="/logout"><button class="logout-button" type="submit">Déconnexion</button></form>
          </nav>"""

    if auth:
        page = f"<main class='auth-page'>{body}</main>"
    else:
        page = f"""
        <div class="shell">
          <header class="topbar">
            <div class="topbar-inner">
              <a class="brand" href="/" aria-label="{APP_NAME}">
                <span class="logo-crop brand-logo"><img src="/static/brand/mediascribe-logo.png" alt="{APP_NAME}"></span>
              </a>
              {nav}
            </div>
          </header>
          <main class="container">{body}</main>
        </div>"""

    return HTMLResponse(
        f"""<!doctype html>
<html lang="fr" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} - {APP_NAME}</title>
  <link rel="stylesheet" href="/static/css/app.css">
  <script src="/static/js/app.js" defer></script>
</head>
<body>
  {page}
</body>
</html>"""
    )


def auth_card(title: str, subtitle: str, form_html: str, error: str | None = None) -> HTMLResponse:
    error_html = f"<p class='error'>{esc(error)}</p>" if error else ""
    return base_document(
        title,
        f"""
        <section class="auth-card">
          <span class="logo-crop auth-logo"><img src="/static/brand/mediascribe-logo.png" alt="{APP_NAME}"></span>
          <h1 class="auth-title">{esc(title)}</h1>
          <p class="auth-subtitle">{esc(subtitle)}</p>
          {error_html}
          {form_html}
        </section>""",
        auth=True,
    )


def current_user(session: str | None = Cookie(default=None)) -> dict | None:
    secret = get_setting("secret_key")
    user_id = verify_session(session, secret)
    if not user_id:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ? AND enabled = 1", (user_id,)).fetchone()
    return dict(row) if row else None


def require_user(session: str | None = Cookie(default=None)) -> dict:
    user = current_user(session)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def require_admin(session: str | None = Cookie(default=None)) -> dict:
    user = require_user(session)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_regular_user(session: str | None = Cookie(default=None)) -> dict:
    user = require_user(session)
    if user["role"] == "admin":
        raise HTTPException(status_code=303, headers={"Location": "/admin"})
    return user


def user_jobs(user_id: int, *, limit: int | None = None):
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    query = f"""
        SELECT
            j.*,
            (
                SELECT COUNT(*)
                FROM transcription_jobs j2
                WHERE j2.user_id = j.user_id
                  AND (j2.created_at < j.created_at OR (j2.created_at = j.created_at AND j2.id <= j.id))
            ) AS user_job_number
        FROM transcription_jobs j
        WHERE j.user_id = ?
        ORDER BY j.created_at DESC, j.id DESC
        {limit_sql}
    """
    with connect() as conn:
        return conn.execute(query, (user_id,)).fetchall()


def history_sidebar(rows) -> str:
    if not rows:
        return "<div class='history-empty'>Aucune transcription.</div>"
    items = []
    for row in rows:
        source = "Live" if row["source_type"] == "live" else "Fichier"
        items.append(
            f"""
            <a class="history-item" href="/jobs/{row['id']}">
              <span class="history-title">#{row['user_job_number']} - {esc(row['original_filename'])}</span>
              <span class="history-meta">{source} - {esc(status_label(row['status']))} - {esc(format_datetime(row['created_at']))}</span>
            </a>"""
        )
    return "".join(items)


def progress_html(row) -> str:
    percent = max(0, min(100, int(row["progress_percent"] or 0)))
    stage = row["progress_stage"] or status_label(row["status"])
    return f"""
      <div class="progress-block" data-job-progress>
        <div class="progress-head"><span data-progress-stage>{esc(stage)}</span><strong data-progress-percent>{percent}%</strong></div>
        <div class="progress-track"><span data-progress-bar style="width: {percent}%"></span></div>
      </div>"""


def ensure_mp3_export(row) -> Path:
    media_path = Path(row["media_path"] or "")
    if not media_path.exists():
        raise HTTPException(status_code=404, detail="Audio source not found")
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = EXPORT_DIR / f"job-{row['id']}.mp3"
    if target.exists() and target.stat().st_mtime >= media_path.stat().st_mtime:
        return target
    tmp = target.with_suffix(".mp3.part")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "3",
        "-f",
        "mp3",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        tmp.replace(target)
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise HTTPException(status_code=500, detail=detail[-1000:]) from exc
    return target


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str | None = None, session: str | None = Cookie(default=None)):
    user = current_user(session)
    if user:
        return RedirectResponse("/admin" if user["role"] == "admin" else "/", status_code=303)
    message = "Identifiant ou mot de passe incorrect." if error else None
    return auth_card(
        "Connexion",
        "Accédez à votre espace de transcription local.",
        """
        <form method="post" action="/login">
          <label>Utilisateur</label>
          <input name="username" autocomplete="username" required>
          <label>Mot de passe</label>
          <input name="password" type="password" autocomplete="current-password" required>
          <div class="actions">
            <button type="submit">Connexion</button>
            <a class="button secondary" href="/register">Créer un compte</a>
          </div>
        </form>""",
        message,
    )


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ? AND enabled = 1", (username.strip(),)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return RedirectResponse("/login?error=1", status_code=303)
    redirect = RedirectResponse("/admin" if row["role"] == "admin" else "/", status_code=303)
    redirect.set_cookie("session", sign_session(row["id"], get_setting("secret_key")), httponly=True, secure=False, samesite="lax")
    return redirect


@app.post("/logout")
def logout() -> RedirectResponse:
    redirect = RedirectResponse("/login", status_code=303)
    redirect.delete_cookie("session")
    return redirect


@app.get("/register", response_class=HTMLResponse)
def register_page(error: str | None = None) -> HTMLResponse:
    messages = {
        "invalid": "Le nom d'utilisateur doit faire au moins 3 caractères et le mot de passe au moins 8.",
        "exists": "Ce nom d'utilisateur existe déjà.",
    }
    return auth_card(
        "Créer un compte",
        "Un compte local suffit, aucun email n'est demandé.",
        """
        <form method="post" action="/register">
          <label>Utilisateur</label>
          <input name="username" autocomplete="username" required minlength="3" maxlength="64">
          <label>Mot de passe</label>
          <input name="password" type="password" autocomplete="new-password" required minlength="8">
          <p class="field-help">Minimum 8 caractères. Vous pourrez le changer depuis votre compte.</p>
          <div class="actions">
            <button type="submit">Créer le compte</button>
            <a class="button secondary" href="/login">Connexion</a>
          </div>
        </form>""",
        messages.get(error or ""),
    )


@app.post("/register")
def register(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    username = username.strip()
    if len(username) < 3 or len(password) < 8:
        return RedirectResponse("/register?error=invalid", status_code=303)
    try:
        with connect() as conn:
            conn.execute("INSERT INTO users(username, password_hash, role) VALUES (?, ?, 'user')", (username, hash_password(password)))
    except Exception:
        return RedirectResponse("/register?error=exists", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(session: str | None = Cookie(default=None)) -> HTMLResponse:
    user = require_regular_user(session)
    language = get_setting("default_language", "fr")
    language_options = language_options_html(language)
    try:
        live_chunk_seconds = max(2, min(10, int(get_setting("live_chunk_seconds", "4") or "4")))
    except ValueError:
        live_chunk_seconds = 4
    return base_document(
        "Transcrire",
        f"""
        <div class="workspace single">
          <section class="workspace-main">
            <div class="workspace-heading">
              <p class="eyebrow">Transcription locale</p>
              <h1>Nouvelle transcription</h1>
              <p class="lede">Importez un fichier ou lancez un live transcript rapide depuis votre navigateur. Les traitements restent sur cette machine.</p>
            </div>
            <div class="mode-tabs" role="tablist" aria-label="Mode de transcription">
              <button class="mode-tab active" type="button" data-mode-tab="upload">Fichier audio/vidéo</button>
              <button class="mode-tab" type="button" data-mode-tab="live">Live transcript</button>
            </div>
            <section class="panel mode-panel active" data-mode-panel="upload">
              <form method="post" action="/upload" enctype="multipart/form-data">
                <div class="drop-zone" data-drop-zone>
                  <input type="file" name="file" accept="audio/*,video/*,.mp4,.mov,.mp3,.flac,.wav,.m4a,.aac,.ogg,.mkv,.webm,.avi" required>
                  <div class="drop-icon">+</div>
                  <p class="drop-title">Déposez un fichier ou cliquez ici</p>
                  <p class="drop-help">MP4, MOV, MP3, FLAC, WAV, M4A, AAC, OGG, MKV, WEBM, AVI.</p>
                  <p class="file-name" data-file-name></p>
                </div>
                <label>Langue de transcription</label>
                <select name="language" required>{language_options}</select>
                <div class="actions"><button type="submit">Lancer la transcription</button></div>
              </form>
            </section>
            <section class="panel mode-panel" data-mode-panel="live" data-live-chunk-seconds="{live_chunk_seconds}">
              <div class="live-toolbar">
                <div>
                  <h2>Enregistrement live</h2>
                  <p class="field-help">La transcription travaille pendant l'enregistrement et sera disponible à la fin.</p>
                </div>
                <span class="status" data-live-status>Prêt</span>
              </div>
              <label>Source audio</label>
              <div class="source-grid">
                <label class="source-option"><input type="radio" name="live_mode" value="mic" checked> <span>Micro seul</span></label>
                <label class="source-option"><input type="radio" name="live_mode" value="mic_display"> <span>Micro + partage avec audio</span></label>
              </div>
              <label>Langue de transcription</label>
              <select id="live-language" required>{language_options}</select>
              <div class="live-actions">
                <button type="button" data-live-start>Démarrer</button>
                <button class="secondary" type="button" data-live-stop disabled>Arrêter et sauvegarder</button>
                <button class="secondary" type="button" data-enable-notifications>Notifications</button>
                <button class="danger" type="button" data-live-cancel disabled>Annuler</button>
              </div>
              <div class="meter-grid">
                <div class="audio-meter">
                  <div class="meter-head"><strong>Micro</strong><span data-mic-meter-label>En attente</span></div>
                  <div class="meter-track"><span data-mic-meter></span></div>
                </div>
                <div class="audio-meter">
                  <div class="meter-head"><strong>Audio partagé</strong><span data-share-meter-label>Non demandé</span></div>
                  <div class="meter-track"><span data-share-meter></span></div>
                </div>
              </div>
              <p class="live-message" data-live-message></p>
            </section>
          </section>
        </div>""",
        user=user,
        active="transcribe",
    )


@app.post("/upload")
def upload(file: UploadFile = File(...), language: str = Form("fr"), session: str | None = Cookie(default=None)) -> RedirectResponse:
    user = require_regular_user(session)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file extension")
    max_bytes = int(get_setting("max_upload_mb", "2048")) * 1024 * 1024
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    media_path = UPLOAD_DIR / safe_name
    written = 0
    with media_path.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                media_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="File too large")
            out.write(chunk)
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO transcription_jobs(user_id, original_filename, media_path, language, model_path, status, progress_percent, progress_stage)
               VALUES (?, ?, ?, ?, ?, 'queued', 0, 'En attente')""",
            (user["id"], file.filename or safe_name, str(media_path), language.strip() or "fr", get_setting("model_path")),
        )
        job_id = cur.lastrowid
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/live/start")
def live_start(mode: str = Form("mic"), language: str = Form("fr"), session: str | None = Cookie(default=None)) -> JSONResponse:
    user = require_regular_user(session)
    if mode not in {"mic", "mic_display"}:
        raise HTTPException(status_code=400, detail="Invalid live mode")
    if language not in dict(LANGUAGE_OPTIONS):
        language = get_setting("default_language", "fr")
    session_id = uuid.uuid4().hex
    session_dir = LIVE_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    final_media_path = UPLOAD_DIR / f"live-{session_id}.webm"
    original_name = f"Live transcript {datetime.now(PARIS_TZ).strftime('%d/%m/%y %H:%M:%S')}"
    model_path = get_setting("live_model_path") or get_setting("model_path")
    if not Path(model_path).exists():
        model_path = get_setting("model_path")
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO transcription_jobs(
                user_id, original_filename, media_path, language, model_path, source_type, live_session_id,
                status, progress_percent, progress_stage, started_at
            )
            VALUES (?, ?, ?, ?, ?, 'live', ?, 'running', 1, 'Live en cours', CURRENT_TIMESTAMP)
            """,
            (user["id"], original_name, str(final_media_path), language, model_path, session_id),
        )
        job_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO live_sessions(id, job_id, user_id, mode, status, language, model_path, final_media_path)
            VALUES (?, ?, ?, ?, 'recording', ?, ?, ?)
            """,
            (session_id, job_id, user["id"], mode, language, model_path, str(final_media_path)),
        )
        user_job_number = get_user_job_number(conn, user["id"], job_id)
    return JSONResponse({"session_id": session_id, "job_id": job_id, "user_job_number": user_job_number})


@app.post("/live/{session_id}/chunks")
def live_chunk(
    session_id: str,
    sequence: int = Form(...),
    chunk: UploadFile = File(...),
    session: str | None = Cookie(default=None),
) -> JSONResponse:
    user = require_regular_user(session)
    with connect() as conn:
        live = conn.execute("SELECT * FROM live_sessions WHERE id = ? AND user_id = ?", (session_id, user["id"])).fetchone()
    if not live:
        raise HTTPException(status_code=404)
    if live["status"] != "recording":
        return JSONResponse({"accepted": False, "status": live["status"]})
    safe_sequence = max(0, int(sequence))
    session_dir = LIVE_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = session_dir / f"{safe_sequence:06d}.webm"
    max_bytes = 128 * 1024 * 1024
    written = 0
    with chunk_path.open("wb") as out:
        while data := chunk.file.read(1024 * 1024):
            written += len(data)
            if written > max_bytes:
                chunk_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Live chunk too large")
            out.write(data)
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO live_chunks(session_id, sequence, chunk_path, status)
            VALUES (?, ?, ?, 'queued')
            """,
            (session_id, safe_sequence, str(chunk_path)),
        )
        conn.execute(
            "UPDATE live_sessions SET chunk_count = MAX(chunk_count, ?) WHERE id = ?",
            (safe_sequence + 1, session_id),
        )
    return JSONResponse({"accepted": True})


@app.get("/live/{session_id}/status")
def live_status(session_id: str, session: str | None = Cookie(default=None)) -> JSONResponse:
    user = require_regular_user(session)
    with connect() as conn:
        live = conn.execute("SELECT * FROM live_sessions WHERE id = ? AND user_id = ?", (session_id, user["id"])).fetchone()
        if not live:
            raise HTTPException(status_code=404)
        job = conn.execute("SELECT * FROM transcription_jobs WHERE id = ?", (live["job_id"],)).fetchone()
        user_job_number = get_user_job_number(conn, user["id"], live["job_id"])
    return JSONResponse(
        {
            "session_id": session_id,
            "job_id": live["job_id"],
            "user_job_number": user_job_number,
            "status": live["status"],
            "job_status": job["status"] if job else "failed",
            "transcript_text": job["transcript_text"] if job else "",
            "error": live["error"] or (job["error"] if job else ""),
            "chunk_count": live["chunk_count"],
        }
    )


@app.post("/live/{session_id}/stop")
def live_stop(session_id: str, session: str | None = Cookie(default=None)) -> JSONResponse:
    user = require_regular_user(session)
    with connect() as conn:
        live = conn.execute("SELECT * FROM live_sessions WHERE id = ? AND user_id = ?", (session_id, user["id"])).fetchone()
        if not live:
            raise HTTPException(status_code=404)
        if live["status"] == "recording":
            conn.execute("UPDATE live_sessions SET status = 'stopping', stopped_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    return JSONResponse({"stopping": True, "job_id": live["job_id"]})


@app.post("/live/{session_id}/cancel")
def live_cancel(session_id: str, session: str | None = Cookie(default=None)) -> JSONResponse:
    user = require_regular_user(session)
    with connect() as conn:
        live = conn.execute("SELECT * FROM live_sessions WHERE id = ? AND user_id = ?", (session_id, user["id"])).fetchone()
        if not live:
            raise HTTPException(status_code=404)
        chunks = conn.execute("SELECT chunk_path FROM live_chunks WHERE session_id = ?", (session_id,)).fetchall()
        for chunk_row in chunks:
            Path(chunk_row["chunk_path"]).unlink(missing_ok=True)
        Path(live["final_media_path"]).unlink(missing_ok=True)
        shutil.rmtree(LIVE_DIR / session_id, ignore_errors=True)
        conn.execute("DELETE FROM transcription_jobs WHERE id = ?", (live["job_id"],))
    return JSONResponse({"cancelled": True})


@app.get("/jobs", response_class=HTMLResponse)
def jobs(session: str | None = Cookie(default=None)) -> HTMLResponse:
    user = require_regular_user(session)
    rows = user_jobs(user["id"])
    if rows:
        lines = "".join(
            f"""
            <tr>
              <td><a href="/jobs/{row['id']}">#{row['user_job_number']}</a></td>
              <td>{esc(row['original_filename'])}</td>
              <td>{'Live' if row['source_type'] == 'live' else 'Fichier'}</td>
              <td><span class="status {status_class(row['status'])}">{esc(status_label(row['status']))}</span>{progress_html(row) if row['status'] in {'queued', 'running'} else ''}</td>
              <td>{esc(format_datetime(row['created_at']))}</td>
              <td>
                <form class="inline-form" method="post" action="/jobs/{row['id']}/delete">
                  <button class="danger compact" type="submit">Supprimer</button>
                </form>
              </td>
            </tr>"""
            for row in rows
        )
        content = f"""
        <div class="table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Nom</th><th>Type</th><th>Statut</th><th>Date</th><th>Action</th></tr></thead>
            <tbody>{lines}</tbody>
          </table>
        </div>"""
    else:
        content = "<div class='empty'>Aucune transcription pour le moment.</div>"
    return base_document("Historique", f"<section class='panel'><h2>Historique</h2>{content}</section>", user=user, active="jobs")


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int, session: str | None = Cookie(default=None)) -> HTMLResponse:
    user = require_regular_user(session)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                j.*,
                (
                    SELECT COUNT(*)
                    FROM transcription_jobs j2
                    WHERE j2.user_id = j.user_id
                      AND (j2.created_at < j.created_at OR (j2.created_at = j.created_at AND j2.id <= j.id))
                ) AS user_job_number
            FROM transcription_jobs j
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404)
    transcript = esc(row["transcript_text"] or "")
    error = f"<p class='error'>{esc(row['error'])}</p>" if row["error"] else ""
    download = f"<a class='button secondary' href='/jobs/{job_id}/download.txt'>Télécharger TXT</a>" if row["status"] == "completed" else ""
    audio_download = f"<a class='button secondary' href='/jobs/{job_id}/download.mp3'>Télécharger MP3</a>" if row["status"] == "completed" and row["media_path"] else ""
    notifications = (
        """<button class="secondary" type="button" data-enable-notifications>Activer notifications</button>
           <span class="small-muted" data-notification-feedback></span>"""
        if row["status"] in {"queued", "running"}
        else ""
    )
    body = f"""
        <section class="panel" data-job-detail="{job_id}">
          <h2>Transcription #{row['user_job_number']}</h2>
          <div class="job-meta">
            <div class="meta-item"><span>Nom</span><strong>{esc(row['original_filename'])}</strong></div>
            <div class="meta-item"><span>Type</span><strong>{'Live' if row['source_type'] == 'live' else 'Fichier'}</strong></div>
            <div class="meta-item"><span>Statut</span><strong><span class="status {status_class(row['status'])}">{esc(status_label(row['status']))}</span></strong></div>
            <div class="meta-item"><span>Langue</span><strong>{esc(language_label(row['language']))}</strong></div>
            <div class="meta-item"><span>Créé</span><strong>{esc(format_datetime(row['created_at']))}</strong></div>
          </div>
          {progress_html(row)}
          {error}
          <label>Texte transcrit</label>
          <textarea id="transcript" readonly>{transcript}</textarea>
          <div class="actions">
            <button type="button" onclick="copyTranscript()">Copier</button>
            {notifications}
            {download}
            {audio_download}
            <form method="post" action="/jobs/{job_id}/delete"><button class="danger" type="submit">Supprimer</button></form>
            <span class="copy-confirm" data-copy-confirm>Copié</span>
          </div>
        </section>"""
    return base_document(f"Job {job_id}", body, user=user, active="jobs")


@app.get("/jobs/{job_id}/status")
def job_status(job_id: int, session: str | None = Cookie(default=None)) -> JSONResponse:
    user = require_regular_user(session)
    with connect() as conn:
        row = conn.execute("SELECT * FROM transcription_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404)
    return JSONResponse(
        {
            "id": job_id,
            "status": row["status"],
            "status_label": status_label(row["status"]),
            "progress_percent": int(row["progress_percent"] or 0),
            "progress_stage": row["progress_stage"] or status_label(row["status"]),
            "transcript_text": row["transcript_text"] or "",
            "error": row["error"] or "",
        }
    )


@app.get("/jobs/{job_id}/download.txt")
def download(job_id: int, session: str | None = Cookie(default=None)) -> PlainTextResponse:
    user = require_regular_user(session)
    with connect() as conn:
        row = conn.execute("SELECT * FROM transcription_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404)
    headers = {"Content-Disposition": f'attachment; filename="transcription-{job_id}.txt"'}
    return PlainTextResponse(row["transcript_text"] or "", headers=headers)


@app.get("/jobs/{job_id}/download.mp3")
def download_mp3(job_id: int, session: str | None = Cookie(default=None)) -> FileResponse:
    user = require_regular_user(session)
    with connect() as conn:
        row = conn.execute("SELECT * FROM transcription_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404)
    if row["status"] != "completed":
        raise HTTPException(status_code=409, detail="Job is not completed")
    target = ensure_mp3_export(row)
    return FileResponse(
        target,
        media_type="audio/mpeg",
        filename=f"mediascribe-job-{job_id}.mp3",
    )


@app.post("/jobs/{job_id}/delete")
def delete_job(job_id: int, session: str | None = Cookie(default=None)) -> RedirectResponse:
    user = require_regular_user(session)
    with connect() as conn:
        row = conn.execute("SELECT * FROM transcription_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or row["user_id"] != user["id"]:
            raise HTTPException(status_code=404)
        Path(row["media_path"]).unlink(missing_ok=True)
        if row["transcript_path"]:
            Path(row["transcript_path"]).unlink(missing_ok=True)
        (EXPORT_DIR / f"job-{job_id}.mp3").unlink(missing_ok=True)
        if row["live_session_id"]:
            chunks = conn.execute("SELECT chunk_path FROM live_chunks WHERE session_id = ?", (row["live_session_id"],)).fetchall()
            for chunk in chunks:
                Path(chunk["chunk_path"]).unlink(missing_ok=True)
            shutil.rmtree(LIVE_DIR / row["live_session_id"], ignore_errors=True)
        conn.execute("DELETE FROM transcription_jobs WHERE id = ?", (job_id,))
    return RedirectResponse("/jobs", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account(error: str | None = None, session: str | None = Cookie(default=None)) -> HTMLResponse:
    user = require_regular_user(session)
    error_html = "<p class='error'>Le mot de passe doit contenir au moins 8 caractères.</p>" if error else ""
    return base_document(
        "Compte",
        f"""
        <section class="panel">
          <p class="eyebrow">Profil local</p>
          <h2>Compte {esc(user['username'])}</h2>
          {error_html}
          <form method="post" action="/account/password">
            <label>Nouveau mot de passe</label>
            <input name="password" type="password" required minlength="8" autocomplete="new-password">
            <p class="field-help">Le changement est immédiat et ne nécessite pas d'email.</p>
            <div class="actions"><button type="submit">Changer le mot de passe</button></div>
          </form>
        </section>""",
        user=user,
        active="account",
    )


@app.post("/account/password")
def account_password(password: str = Form(...), session: str | None = Cookie(default=None)) -> RedirectResponse:
    user = require_regular_user(session)
    if len(password) < 8:
        return RedirectResponse("/account?error=short", status_code=303)
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
    return RedirectResponse("/account", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin(session: str | None = Cookie(default=None)) -> HTMLResponse:
    user = require_admin(session)
    settings = get_settings()
    with connect() as conn:
        users = conn.execute("SELECT id, username, role, enabled, created_at FROM users ORDER BY created_at DESC").fetchall()
    user_rows = "".join(
        f"""
        <tr>
          <td>{row['id']}</td>
          <td>{esc(row['username'])}</td>
          <td>{esc(row['role'])}</td>
          <td><span class="status {'completed' if row['enabled'] else 'failed'}">{'Actif' if row['enabled'] else 'Désactivé'}</span></td>
          <td><form method="post" action="/admin/users/{row['id']}/toggle"><button class="secondary" type="submit">Basculer</button></form></td>
        </tr>"""
        for row in users
    )
    stats = machine_stats()
    current_model = settings.get("model_path", "")
    live_model = settings.get("live_model_path", current_model)
    current_model_path = Path(current_model) if current_model else None
    current_model_label = current_model_path.name if current_model_path else "Aucun modèle"
    current_model_size = human_size(current_model_path.stat().st_size) if current_model_path and current_model_path.exists() else "-"
    current_state, current_state_label = model_resource_state(model_profile_for_path(current_model_path) if current_model_path else None)
    body = f"""
        <section class="panel admin-hero">
          <p class="eyebrow">Administration</p>
          <h1>Paramètres MediaScribe</h1>
          <p class="lede">Pilotez le modèle, la langue par défaut, les utilisateurs et l'état de la machine depuis cet espace dédié.</p>
        </section>
        <div class="admin-grid">
          <section class="panel">
            <p class="eyebrow">Configuration</p>
            <h2>Paramètres de transcription</h2>
            <form method="post" action="/admin/settings">
              <div class="settings-grid">
                <div><label>Langue par défaut</label><select name="default_language" required>{language_options_html(settings.get('default_language', 'fr'))}</select></div>
                <div><label>Taille max upload MB</label><input name="max_upload_mb" type="number" min="1" value="{esc(settings.get('max_upload_mb'))}"></div>
                <div><label>Jobs simultanés</label><input name="max_concurrent_jobs" type="number" min="1" value="{esc(settings.get('max_concurrent_jobs'))}"></div>
                <div><label>Binaire whisper.cpp</label><input name="whisper_binary" value="{esc(settings.get('whisper_binary'))}"></div>
              </div>
              <label>Modèle actif</label>
              <select name="model_path" required>{model_options_html(current_model)}</select>
              <p class="field-help model-state {current_state}">Actuel : {esc(current_model_label)} ({esc(current_model_size)}) - {esc(current_state_label)} - {esc(current_model)}</p>
              <label>Modèle live rapide</label>
              <select name="live_model_path" required>{model_options_html(live_model)}</select>
              <p class="field-help">Utilisez tiny ou base pour viser une latence proche de 5 secondes.</p>
              <label>Durée des chunks live</label>
              <input name="live_chunk_seconds" type="number" min="2" max="10" value="{esc(settings.get('live_chunk_seconds', '4'))}">
              <div class="actions"><button type="submit">Enregistrer</button></div>
            </form>
          </section>
          <div>
            <section class="panel">
              <p class="eyebrow">Sécurité</p>
              <h2>Mot de passe admin</h2>
              <form method="post" action="/admin/password">
                <label>Nouveau mot de passe</label>
                <input name="password" type="password" required minlength="8" autocomplete="new-password">
                <div class="actions"><button type="submit">Changer le mot de passe</button></div>
              </form>
            </section>
            <section class="panel">
              <p class="eyebrow">Modèle local</p>
              <h2>Modèles Whisper</h2>
              <div class="model-list">{model_catalog_html(current_model)}</div>
              <form method="post" action="/admin/models/upload" enctype="multipart/form-data">
                <div class="drop-zone" data-drop-zone>
                  <input type="file" name="file" accept=".bin,.gguf" required>
                  <div class="drop-icon">+</div>
                  <p class="drop-title">Ajouter un modèle Whisper</p>
                  <p class="drop-help">Fichiers `.bin` ou `.gguf` compatibles whisper.cpp.</p>
                  <p class="file-name" data-file-name></p>
                </div>
                <div class="actions"><button type="submit">Uploader et activer</button></div>
              </form>
            </section>
            <section class="panel">
              <p class="eyebrow">Machine</p>
              <h2>État système</h2>
              <div class="system-grid">
                <div class="stat"><strong>{esc(stats['cpu'])}</strong><span>Coeurs CPU</span></div>
                <div class="stat"><strong>{esc(stats['ram_total'])}</strong><span>RAM totale</span></div>
                <div class="stat"><strong>{esc(stats['ram_used'])}</strong><span>RAM utilisée</span></div>
                <div class="stat"><strong>{esc(stats['ram_available'])}</strong><span>RAM disponible</span></div>
                <div class="stat"><strong>{esc(stats['disk_total'])}</strong><span>Stockage total</span></div>
                <div class="stat"><strong>{esc(stats['disk_used'])}</strong><span>Stockage utilisé</span></div>
                <div class="stat"><strong>{esc(stats['disk_free'])}</strong><span>Stockage restant</span></div>
                <div class="stat"><strong>{esc(language_label(settings.get('default_language', 'fr')))}</strong><span>Langue active</span></div>
              </div>
            </section>
          </div>
        </div>
        <section class="panel">
          <h2>Utilisateurs</h2>
          <div class="table-wrap">
            <table>
              <thead><tr><th>ID</th><th>Utilisateur</th><th>Rôle</th><th>État</th><th>Action</th></tr></thead>
              <tbody>{user_rows}</tbody>
            </table>
          </div>
        </section>"""
    return base_document("Admin", body, user=user, active="admin")


@app.post("/admin/settings")
def admin_settings(
    default_language: str = Form(...),
    model_path: str = Form(...),
    live_model_path: str = Form(...),
    live_chunk_seconds: str = Form("4"),
    whisper_binary: str = Form(...),
    max_upload_mb: str = Form(...),
    max_concurrent_jobs: str = Form(...),
    session: str | None = Cookie(default=None),
) -> RedirectResponse:
    require_admin(session)
    if default_language not in dict(LANGUAGE_OPTIONS):
        default_language = "fr"
    selected_model = Path(model_path)
    if selected_model.suffix.lower() not in {".bin", ".gguf"} or not selected_model.exists():
        selected_model = Path(get_setting("model_path"))
    selected_live_model = Path(live_model_path)
    if selected_live_model.suffix.lower() not in {".bin", ".gguf"} or not selected_live_model.exists():
        selected_live_model = selected_model
    try:
        chunk_seconds = str(max(2, min(10, int(live_chunk_seconds))))
    except ValueError:
        chunk_seconds = "4"
    for key, value in {
        "default_language": default_language,
        "model_path": str(selected_model),
        "live_model_path": str(selected_live_model),
        "live_chunk_seconds": chunk_seconds,
        "whisper_binary": whisper_binary,
        "max_upload_mb": max_upload_mb,
        "max_concurrent_jobs": max_concurrent_jobs,
    }.items():
        set_setting(key, value.strip())
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/password")
def admin_password(password: str = Form(...), session: str | None = Cookie(default=None)) -> RedirectResponse:
    user = require_admin(session)
    if len(password) >= 8:
        with connect() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/models/upload")
def upload_model(file: UploadFile = File(...), session: str | None = Cookie(default=None)) -> RedirectResponse:
    require_admin(session)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".bin", ".gguf"}:
        raise HTTPException(status_code=400, detail="Unsupported model extension")
    target = MODEL_DIR / Path(file.filename or f"model{suffix}").name
    with target.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            out.write(chunk)
    set_setting("model_path", str(target))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/models/activate")
def activate_model(model_path: str = Form(...), session: str | None = Cookie(default=None)) -> RedirectResponse:
    require_admin(session)
    selected = Path(model_path)
    if selected.exists() and selected.suffix.lower() in {".bin", ".gguf"}:
        set_setting("model_path", str(selected))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/models/download")
def download_catalog_model(model_key: str = Form(...), session: str | None = Cookie(default=None)) -> RedirectResponse:
    require_admin(session)
    profile = MODEL_CATALOG.get(model_key)
    if not profile:
        raise HTTPException(status_code=400, detail="Unknown model")
    target = MODEL_DIR / profile["filename"]
    if not target.exists():
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            urllib.request.urlretrieve(profile["url"], tmp)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
    set_setting("model_path", str(target))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/toggle")
def toggle_user(user_id: int, session: str | None = Cookie(default=None)) -> RedirectResponse:
    admin_user = require_admin(session)
    if user_id == admin_user["id"]:
        return RedirectResponse("/admin", status_code=303)
    with connect() as conn:
        conn.execute("UPDATE users SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (user_id,))
    return RedirectResponse("/admin", status_code=303)
