from __future__ import annotations

import html
import shutil
import uuid
from pathlib import Path

from fastapi import Cookie, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import ALLOWED_EXTENSIONS, APP_NAME, MODEL_DIR, UPLOAD_DIR, ensure_directories
from .db import connect, get_setting, get_settings, init_db, set_setting
from .security import hash_password, sign_session, verify_password, verify_session


app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


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


def nav_link(path: str, label: str, active: str, key: str) -> str:
    selected = " active" if active == key else ""
    return f"<a class='nav-link{selected}' href='{path}'>{label}</a>"


def base_document(title: str, body: str, *, user: dict | None = None, active: str = "", auth: bool = False) -> HTMLResponse:
    nav = ""
    if user:
        admin = nav_link("/admin", "Admin", active, "admin") if user["role"] == "admin" else ""
        nav = f"""
        <nav class="nav" aria-label="Navigation principale">
          {nav_link("/", "Transcrire", active, "transcribe")}
          {nav_link("/jobs", "Historique", active, "jobs")}
          {nav_link("/account", "Compte", active, "account")}
          {admin}
          <button class="theme-toggle" type="button" onclick="toggleTheme()">Mode <span data-theme-label>Sombre</span></button>
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


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str | None = None, session: str | None = Cookie(default=None)):
    if current_user(session):
        return RedirectResponse("/", status_code=303)
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
    redirect = RedirectResponse("/", status_code=303)
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
    user = require_user(session)
    language = get_setting("default_language", "fr")
    language_options = "".join(
        f"<option value='{code}' {'selected' if code == language else ''}>{label}</option>"
        for code, label in [
            ("fr", "Français"),
            ("en", "Anglais"),
            ("es", "Espagnol"),
            ("de", "Allemand"),
            ("it", "Italien"),
            ("pt", "Portugais"),
            ("auto", "Détection automatique"),
        ]
    )
    return base_document(
        "Transcrire",
        f"""
        <div class="hero-grid">
          <section class="hero-panel">
            <div>
              <p class="eyebrow">Transcription locale</p>
              <h1>Convertissez vos audios et vidéos en texte.</h1>
              <p class="lede">Déposez un fichier, choisissez la langue, puis récupérez une transcription consultable, copiable et téléchargeable depuis votre historique.</p>
            </div>
            <div class="stat-row">
              <div class="stat"><strong>100% local</strong><span>Aucune API externe</span></div>
              <div class="stat"><strong>MP3 / MP4</strong><span>Et formats courants</span></div>
              <div class="stat"><strong>TXT</strong><span>Copie et export</span></div>
            </div>
          </section>
          <section class="panel">
            <h2>Nouvelle transcription</h2>
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
              <p class="field-help">Le français est sélectionné par défaut. Utilisez la détection automatique si la langue varie.</p>
              <div class="actions"><button type="submit">Lancer la transcription</button></div>
            </form>
          </section>
        </div>""",
        user=user,
        active="transcribe",
    )


@app.post("/upload")
def upload(file: UploadFile = File(...), language: str = Form("fr"), session: str | None = Cookie(default=None)) -> RedirectResponse:
    user = require_user(session)
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
            """INSERT INTO transcription_jobs(user_id, original_filename, media_path, language, model_path, status)
               VALUES (?, ?, ?, ?, ?, 'queued')""",
            (user["id"], file.filename or safe_name, str(media_path), language.strip() or "fr", get_setting("model_path")),
        )
        job_id = cur.lastrowid
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def jobs(session: str | None = Cookie(default=None)) -> HTMLResponse:
    user = require_user(session)
    if user["role"] == "admin":
        query = "SELECT j.*, u.username FROM transcription_jobs j JOIN users u ON u.id = j.user_id ORDER BY j.created_at DESC"
        params = ()
    else:
        query = "SELECT j.*, u.username FROM transcription_jobs j JOIN users u ON u.id = j.user_id WHERE j.user_id = ? ORDER BY j.created_at DESC"
        params = (user["id"],)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    if rows:
        lines = "".join(
            f"""
            <tr>
              <td><a href="/jobs/{row['id']}">#{row['id']}</a></td>
              <td>{esc(row['original_filename'])}</td>
              <td>{esc(row['username'])}</td>
              <td><span class="status {status_class(row['status'])}">{esc(status_label(row['status']))}</span></td>
              <td>{esc(row['created_at'])}</td>
            </tr>"""
            for row in rows
        )
        content = f"""
        <div class="table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Fichier</th><th>Utilisateur</th><th>Statut</th><th>Date</th></tr></thead>
            <tbody>{lines}</tbody>
          </table>
        </div>"""
    else:
        content = "<div class='empty'>Aucune transcription pour le moment.</div>"
    return base_document("Historique", f"<section class='panel'><h2>Historique</h2>{content}</section>", user=user, active="jobs")


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int, session: str | None = Cookie(default=None)) -> HTMLResponse:
    user = require_user(session)
    with connect() as conn:
        row = conn.execute("SELECT * FROM transcription_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
        raise HTTPException(status_code=404)
    refresh = "<meta http-equiv='refresh' content='5'>" if row["status"] in {"queued", "running"} else ""
    transcript = esc(row["transcript_text"] or "")
    error = f"<p class='error'>{esc(row['error'])}</p>" if row["error"] else ""
    download = f"<a class='button secondary' href='/jobs/{job_id}/download.txt'>Télécharger TXT</a>" if row["status"] == "completed" else ""
    body = f"""{refresh}
        <section class="panel">
          <h2>Transcription #{job_id}</h2>
          <div class="job-meta">
            <div class="meta-item"><span>Fichier</span><strong>{esc(row['original_filename'])}</strong></div>
            <div class="meta-item"><span>Statut</span><strong><span class="status {status_class(row['status'])}">{esc(status_label(row['status']))}</span></strong></div>
            <div class="meta-item"><span>Langue</span><strong>{esc(row['language'])}</strong></div>
            <div class="meta-item"><span>Créé</span><strong>{esc(row['created_at'])}</strong></div>
          </div>
          {error}
          <label>Texte transcrit</label>
          <textarea id="transcript" readonly>{transcript}</textarea>
          <div class="actions">
            <button type="button" onclick="copyTranscript()">Copier</button>
            {download}
            <form method="post" action="/jobs/{job_id}/delete"><button class="danger" type="submit">Supprimer</button></form>
            <span class="copy-confirm" data-copy-confirm>Copié</span>
          </div>
        </section>"""
    return base_document(f"Job {job_id}", body, user=user, active="jobs")


@app.get("/jobs/{job_id}/download.txt")
def download(job_id: int, session: str | None = Cookie(default=None)) -> PlainTextResponse:
    user = require_user(session)
    with connect() as conn:
        row = conn.execute("SELECT * FROM transcription_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
        raise HTTPException(status_code=404)
    headers = {"Content-Disposition": f'attachment; filename="transcription-{job_id}.txt"'}
    return PlainTextResponse(row["transcript_text"] or "", headers=headers)


@app.post("/jobs/{job_id}/delete")
def delete_job(job_id: int, session: str | None = Cookie(default=None)) -> RedirectResponse:
    user = require_user(session)
    with connect() as conn:
        row = conn.execute("SELECT * FROM transcription_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
            raise HTTPException(status_code=404)
        Path(row["media_path"]).unlink(missing_ok=True)
        if row["transcript_path"]:
            Path(row["transcript_path"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM transcription_jobs WHERE id = ?", (job_id,))
    return RedirectResponse("/jobs", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account(error: str | None = None, session: str | None = Cookie(default=None)) -> HTMLResponse:
    user = require_user(session)
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
    user = require_user(session)
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
    disk = shutil.disk_usage("/")
    body = f"""
        <div class="admin-grid">
          <section class="panel">
            <p class="eyebrow">Configuration</p>
            <h2>Paramètres de transcription</h2>
            <form method="post" action="/admin/settings">
              <div class="settings-grid">
                <div><label>Langue par défaut</label><input name="default_language" value="{esc(settings.get('default_language'))}"></div>
                <div><label>Taille max upload MB</label><input name="max_upload_mb" type="number" min="1" value="{esc(settings.get('max_upload_mb'))}"></div>
                <div><label>Jobs simultanés</label><input name="max_concurrent_jobs" type="number" min="1" value="{esc(settings.get('max_concurrent_jobs'))}"></div>
                <div><label>Binaire whisper.cpp</label><input name="whisper_binary" value="{esc(settings.get('whisper_binary'))}"></div>
              </div>
              <label>Chemin modèle</label>
              <input name="model_path" value="{esc(settings.get('model_path'))}">
              <div class="actions"><button type="submit">Enregistrer</button></div>
            </form>
          </section>
          <div>
            <section class="panel">
              <p class="eyebrow">Modèle local</p>
              <h2>Uploader un modèle</h2>
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
              <div class="stat-row">
                <div class="stat"><strong>{disk.free // (1024 ** 3)} GB</strong><span>Disque libre</span></div>
                <div class="stat"><strong>{disk.total // (1024 ** 3)} GB</strong><span>Disque total</span></div>
                <div class="stat"><strong>{esc(settings.get('default_language'))}</strong><span>Langue</span></div>
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
    whisper_binary: str = Form(...),
    max_upload_mb: str = Form(...),
    max_concurrent_jobs: str = Form(...),
    session: str | None = Cookie(default=None),
) -> RedirectResponse:
    require_admin(session)
    for key, value in {
        "default_language": default_language,
        "model_path": model_path,
        "whisper_binary": whisper_binary,
        "max_upload_mb": max_upload_mb,
        "max_concurrent_jobs": max_concurrent_jobs,
    }.items():
        set_setting(key, value.strip())
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


@app.post("/admin/users/{user_id}/toggle")
def toggle_user(user_id: int, session: str | None = Cookie(default=None)) -> RedirectResponse:
    admin_user = require_admin(session)
    if user_id == admin_user["id"]:
        return RedirectResponse("/admin", status_code=303)
    with connect() as conn:
        conn.execute("UPDATE users SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id = ?", (user_id,))
    return RedirectResponse("/admin", status_code=303)
