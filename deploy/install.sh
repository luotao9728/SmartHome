#!/usr/bin/env bash
# Install or update SmartHome on a Raspberry Pi (Raspberry Pi OS Bookworm).
#
#   sudo ./deploy/install.sh
#
# Idempotent: safe to re-run after a git pull.  It creates a service user, a
# virtualenv, the data directory and a systemd unit, then starts the hub.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/smarthome}"
DATA_DIR="${DATA_DIR:-/var/lib/smarthome}"
SERVICE_USER="${SERVICE_USER:-smarthome}"
PORT="${PORT:-8080}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "run this with sudo" >&2
  exit 1
fi

say "installing packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync curl

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "creating service user $SERVICE_USER"
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "copying application to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'mpyenv' \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'scripts/shots' --exclude 'scripts/*.log' \
  "$SRC_DIR/" "$APP_DIR/"

say "creating virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

say "preparing $DATA_DIR"
mkdir -p "$DATA_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$DATA_DIR"
chmod 700 "$DATA_DIR"

say "installing systemd unit"
sed -e "s#/opt/smarthome#$APP_DIR#g" \
    -e "s#/var/lib/smarthome#$DATA_DIR#g" \
    -e "s#User=smarthome#User=$SERVICE_USER#" \
    -e "s#Group=smarthome#Group=$SERVICE_USER#" \
    -e "s#--port 8080#--port $PORT#" \
    "$APP_DIR/deploy/smarthome.service" > /etc/systemd/system/smarthome.service
systemctl daemon-reload
systemctl enable smarthome.service

say "starting the hub"
# The first start prints the generated admin password to the journal.
systemctl restart smarthome.service
sleep 3

if systemctl is-active --quiet smarthome.service; then
  address="$(hostname -I 2>/dev/null | awk '{print $1}')"
  cat <<EOF

SmartHome is running at http://${address:-<this-pi>}:${PORT}

Next steps:
  1. The generated admin password is in:
       sudo journalctl -u smarthome -n 40 --no-pager | grep -A2 "Admin password"
  2. Open the UI and let one board enrol:
       - press "Add device" -> "Allow new devices", or
       - sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python -m smarthome serve   # (foreground debug)
  3. Useful commands:
       sudo systemctl status smarthome
       sudo journalctl -u smarthome -f
       sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python -m smarthome info
       sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python -m smarthome set-password

EOF
else
  warn "the service failed to start; see: sudo journalctl -u smarthome -n 60 --no-pager"
  exit 1
fi
