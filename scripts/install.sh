#!/usr/bin/env bash
set -euo pipefail

APP_USER="${MEDIASCRIBE_USER:-mediascribe}"
APP_DIR="${MEDIASCRIBE_APP_DIR:-/opt/mediascribe/app}"
BASE_DIR="${MEDIASCRIBE_BASE_DIR:-/opt/mediascribe}"
DATA_DIR="${MEDIASCRIBE_DATA_DIR:-/var/lib/mediascribe}"
ADMIN_PASSWORD="${MEDIASCRIBE_ADMIN_PASSWORD:-ChangeMeNow!}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./scripts/install.sh"
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip ffmpeg build-essential cmake git caddy curl rsync

id -u "${APP_USER}" >/dev/null 2>&1 || useradd --system --home "${DATA_DIR}" --shell /usr/sbin/nologin "${APP_USER}"

mkdir -p "${BASE_DIR}" "${APP_DIR}" "${BASE_DIR}/models" "${DATA_DIR}/uploads" "${DATA_DIR}/transcripts" "${DATA_DIR}/live" "${DATA_DIR}/exports"
rsync -a --delete --exclude '.venv' --exclude 'data' ./ "${APP_DIR}/"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [[ ! -d "${BASE_DIR}/whisper.cpp" ]]; then
  git clone https://github.com/ggerganov/whisper.cpp.git "${BASE_DIR}/whisper.cpp"
fi
cmake -S "${BASE_DIR}/whisper.cpp" -B "${BASE_DIR}/whisper.cpp/build"
cmake --build "${BASE_DIR}/whisper.cpp/build" --config Release -j"$(nproc)"

if [[ ! -f "${BASE_DIR}/models/ggml-small.bin" ]]; then
  echo "Place your model at ${BASE_DIR}/models/ggml-small.bin or upload one from the admin UI."
fi

install -m 0644 "${APP_DIR}/deploy/systemd/mediascribe.service" /etc/systemd/system/mediascribe.service
install -m 0644 "${APP_DIR}/deploy/systemd/mediascribe-worker.service" /etc/systemd/system/mediascribe-worker.service
IP_ADDR="$(hostname -I | awk '{print $1}')"
cat > /etc/caddy/Caddyfile <<CADDY
https://${IP_ADDR}, https://127.0.0.1, https://localhost {
	tls internal
	reverse_proxy 127.0.0.1:8000
}
CADDY

chown -R "${APP_USER}:${APP_USER}" "${BASE_DIR}" "${DATA_DIR}"

runuser -u "${APP_USER}" -- env \
  MEDIASCRIBE_DATA_DIR="${DATA_DIR}" \
  MEDIASCRIBE_MODEL_DIR="${BASE_DIR}/models" \
  MEDIASCRIBE_ADMIN_PASSWORD="${ADMIN_PASSWORD}" \
  bash -c "cd '${APP_DIR}' && '${APP_DIR}/.venv/bin/python' -c 'from app.config import ensure_directories; from app.db import init_db; ensure_directories(); init_db()'"

systemctl daemon-reload
systemctl enable --now mediascribe.service
systemctl enable --now mediascribe-worker.service
systemctl enable --now caddy.service
systemctl reload caddy.service

echo
echo "MediaScribe installed."
echo "URL: https://${IP_ADDR}"
echo "Admin username: admin"
echo "Admin password: ${ADMIN_PASSWORD}"
echo "Change the admin password immediately after first login."
