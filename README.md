# MediaScribe

MediaScribe is a self-hosted local transcription web app for Ubuntu and Debian servers, LXC containers, and virtual machines.

It provides an HTTPS web interface where users can upload audio or video files and transcribe them locally with `whisper.cpp`. No external transcription API is used.

## Features

- Local transcription with `whisper.cpp`.
- HTTPS web UI served through Caddy on port `443`.
- User registration with username and password, no email required.
- Admin account for settings, users, and model management.
- Drag-and-drop upload for common audio and video formats.
- MP3 and MP4 support as primary formats.
- French as the default transcription language.
- Light and dark UI modes with a manual toggle.
- Persistent transcription history per user.
- Copy transcript to clipboard.
- Download transcript as `.txt`.
- Upload and activate a local Whisper model from the admin page.
- Systemd services for the web app and transcription worker.

## Screens and Workflow

1. Open the web interface.
2. Create a user account or log in as admin.
3. Upload an audio or video file.
4. Choose the transcription language.
5. Wait for the job to finish.
6. Copy the result or download it as a text file.

## Supported Formats

MediaScribe accepts these extensions by default:

```text
.mp4 .mov .mkv .webm .avi
.mp3 .flac .wav .m4a .aac .ogg
```

Internally, media files are converted to mono 16 kHz WAV with `ffmpeg`, then processed by `whisper.cpp`.

## Requirements

Recommended system:

- Ubuntu 24.04 LXC/VM or Debian equivalent.
- Root or sudo access.
- At least 2 CPU cores.
- At least 2 GB RAM for the `small` model.
- Enough disk space for uploads and Whisper models.

The installer installs:

- Python 3
- Python venv
- `ffmpeg`
- `git`
- `cmake`
- build tools
- Caddy
- `whisper.cpp`

## Quick Install

Run this on the target machine:

```bash
curl -fsSL https://raw.githubusercontent.com/Vayaris/MediaScribe/main/scripts/bootstrap.sh | sudo env MEDIASCRIBE_REPO_URL=https://github.com/Vayaris/MediaScribe.git bash
```

Then open:

```text
https://<machine-ip>
```

The default deployment on an IP address uses Caddy's internal certificate authority. Your browser will probably show a certificate warning on first access. Accept it to continue, or configure a real domain later.

## Manual Install From a Clone

```bash
git clone https://github.com/Vayaris/MediaScribe.git
cd MediaScribe
sudo ./scripts/install.sh
```

The installer prints the final URL at the end.

## Default Admin Account

```text
username: admin
password: ChangeMeNow!
```

Change this password immediately after the first login.

You can override the initial password during installation:

```bash
sudo MEDIASCRIBE_ADMIN_PASSWORD='your-secure-password' ./scripts/install.sh
```

## Production Paths

Default installation paths:

```text
/opt/mediascribe/app
/opt/mediascribe/models
/opt/mediascribe/whisper.cpp
/var/lib/mediascribe
/var/lib/mediascribe/uploads
/var/lib/mediascribe/transcripts
/var/lib/mediascribe/mediascribe.db
```

Systemd services:

```text
mediascribe.service
mediascribe-worker.service
caddy.service
```

Useful commands:

```bash
sudo systemctl status mediascribe.service mediascribe-worker.service caddy.service
sudo systemctl restart mediascribe.service mediascribe-worker.service
sudo journalctl -u mediascribe.service -f
sudo journalctl -u mediascribe-worker.service -f
```

## Configuration

Most settings can be changed from the admin page.

Important defaults:

```text
default language: fr
model path: /opt/mediascribe/models/ggml-small.bin
upload limit: 2048 MB
concurrent jobs: 1
whisper binary: /opt/mediascribe/whisper.cpp/build/bin/whisper-cli
```

The installer builds `whisper.cpp`. If the default model is missing, place a compatible model at:

```text
/opt/mediascribe/models/ggml-small.bin
```

You can also upload a `.bin` or `.gguf` model from the admin interface.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export MEDIASCRIBE_DATA_DIR=/tmp/mediascribe-dev
export MEDIASCRIBE_MODEL_DIR=/tmp/mediascribe-dev/models
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In another shell, run the worker:

```bash
. .venv/bin/activate
export MEDIASCRIBE_DATA_DIR=/tmp/mediascribe-dev
export MEDIASCRIBE_MODEL_DIR=/tmp/mediascribe-dev/models
python -m app.worker
```

## Project Structure

```text
app/
  main.py            FastAPI routes and HTML rendering
  worker.py          queued transcription worker
  transcriber.py     ffmpeg + whisper.cpp execution
  db.py              SQLite schema and settings helpers
  security.py        password hashing and session signing
  static/            CSS, JS, and MediaScribe logo
deploy/
  caddy/             Caddy configuration template
  systemd/           service units
scripts/
  install.sh         production installer
  bootstrap.sh       curl-based GitHub installer
tests/
  test_security.py
```

## Security Notes

- MediaScribe is designed for local/self-hosted deployments.
- Passwords are hashed with PBKDF2-SHA256.
- Session cookies are HTTP-only.
- Uploaded file names are not used as server-side storage paths.
- User jobs are isolated by account unless the logged-in user is admin.
- Change the default admin password immediately.

## Release v1.0.0

Initial public release:

- local transcription UI
- account creation and login
- admin settings
- upload and history
- transcript copy/download
- dark/light mode
- local logo and responsive UI
- installer for Ubuntu/Debian LXC and VM environments

