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
HOST="${HOST:-0.0.0.0}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=/etc/systemd/system/smarthome.service

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  die "run this with sudo"
fi

# --------------------------------------------------------------------------- #
# packages
# --------------------------------------------------------------------------- #
say "installing packages"
apt-get update -qq
# python3-venv is a separate package on Debian/Ubuntu and is easy to miss.
apt-get install -y -qq python3 python3-venv python3-pip rsync || die "apt-get failed"

PYTHON="$(command -v python3)" || die "python3 not found"
PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "using Python $PY_VERSION ($PYTHON)"
# FastAPI 0.110+ needs 3.8+, but the hub uses dataclasses/typing from 3.9.
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  die "Python 3.9 or newer is required (found $PY_VERSION)"
fi

# --------------------------------------------------------------------------- #
# service user
# --------------------------------------------------------------------------- #
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "creating service user $SERVICE_USER"
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# --------------------------------------------------------------------------- #
# application files
# --------------------------------------------------------------------------- #
say "copying application to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'mpyenv' \
  --exclude 'py39' --exclude 'py312' \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'scripts/shots' --exclude '*.log' --exclude 'config.json' --exclude '*.db*' \
  "$SRC_DIR/" "$APP_DIR/"

# The installer must be runnable after a git clone that lost the mode bit.
chmod +x "$APP_DIR/deploy/install.sh" "$APP_DIR/tools/flash_node.py" 2>/dev/null || true

# --------------------------------------------------------------------------- #
# virtualenv
# --------------------------------------------------------------------------- #
say "creating virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  "$PYTHON" -m venv "$APP_DIR/.venv" || die "could not create the virtualenv (install python3-venv)"
fi
VENV_PY="$APP_DIR/.venv/bin/python"
"$VENV_PY" -m pip install --quiet --upgrade pip wheel
say "installing dependencies (this can take a minute on a Pi)"
"$VENV_PY" -m pip install --quiet -r "$APP_DIR/requirements.txt" || die "pip install failed"
"$VENV_PY" -c 'import fastapi, uvicorn' || die "dependencies are missing from the virtualenv"

# --------------------------------------------------------------------------- #
# data directory
# --------------------------------------------------------------------------- #
say "preparing $DATA_DIR"
mkdir -p "$DATA_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$DATA_DIR"
chmod 700 "$DATA_DIR"

# --------------------------------------------------------------------------- #
# systemd unit
# --------------------------------------------------------------------------- #
say "installing systemd unit"
# The unit template is written for the default paths; rewrite every path and
# the port so $APP_DIR/$DATA_DIR/PORT overrides actually take effect.
sed -e "s#/opt/smarthome#$APP_DIR#g" \
    -e "s#/var/lib/smarthome#$DATA_DIR#g" \
    -e "s#User=smarthome#User=$SERVICE_USER#" \
    -e "s#Group=smarthome#Group=$SERVICE_USER#" \
    -e "s#--port 8080#--port $PORT#" \
    -e "s#--host 0.0.0.0#--host $HOST#" \
    "$APP_DIR/deploy/smarthome.service" > "$UNIT"

# Fail here rather than at 3am on the next reboot.
if ! grep -q "ExecStart=$VENV_PY " "$UNIT"; then
  warn "the unit does not start the virtualenv interpreter; check $UNIT"
fi
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$UNIT" || warn "systemd-analyze reported problems with $UNIT"
fi

systemctl daemon-reload
systemctl enable smarthome.service >/dev/null

# --------------------------------------------------------------------------- #
# start it
# --------------------------------------------------------------------------- #
say "starting the hub"
systemctl restart smarthome.service

# Wait for the service to settle instead of guessing with a fixed sleep.
for _ in $(seq 1 30); do
  state="$(systemctl is-active smarthome.service || true)"
  [[ "$state" == "active" ]] && break
  sleep 1
done

if [[ "$(systemctl is-active smarthome.service || true)" != "active" ]]; then
  warn "the service failed to start"
  echo
  journalctl -u smarthome.service -n 40 --no-pager || true
  exit 1
fi

# It can be "active" for a second before the process crashes; confirm it stays up.
sleep 3
if [[ "$(systemctl is-active smarthome.service || true)" != "active" ]]; then
  warn "the service started and then exited"
  journalctl -u smarthome.service -n 40 --no-pager || true
  exit 1
fi

if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    say "health check passed"
  else
    warn "the service is running but http://127.0.0.1:${PORT}/healthz did not answer"
    warn "check the firewall/port, or run: journalctl -u smarthome -n 40"
  fi
fi

address="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

SmartHome is running at http://${address:-<this-pi>}:${PORT}

Next steps:
  1. The generated admin password is in:
       sudo journalctl -u smarthome -n 60 --no-pager | grep -A2 "Admin password"
  2. Open the UI and let a board enrol with "Add device" -> "Allow new devices".
  3. Useful commands:
       sudo systemctl status smarthome
       sudo journalctl -u smarthome -f
       sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python -m smarthome info
       sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python -m smarthome set-password

EOF
