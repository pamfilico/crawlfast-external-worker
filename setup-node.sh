#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# crawlfast external worker — node provisioner (old or new machines).
#
# Turns a bare Linux/macOS box into a running crawl node. Idempotent: safe to
# re-run to update or reconfigure. Installs prerequisites, writes config.yaml,
# and (optionally) installs a systemd service so the worker runs on boot.
#
# One-time bootstrap on a FRESH machine (no git yet):
#   sudo apt-get update && sudo apt-get install -y git         # Debian/Ubuntu
#   git clone https://github.com/pamfilico/crawlfast-external-worker.git
#   cd crawlfast-external-worker
#   ./setup-node.sh --api-url http://192.168.1.47:5099 --api-key cfw_XXX --name node-1 --service
#
# Flags:
#   --api-url URL     crawlfast API base (required)          e.g. http://192.168.1.47:5099
#   --api-key KEY     this node's worker key (required)      e.g. cfw_...
#   --name NAME       node name (optional; default hostname)
#   --poll SECONDS    poll interval (default 5)
#   --service         install + start a systemd service (auto-restart, boot-start)  [needs sudo]
#   --once            run a single cycle and exit (no service)
#   (no run flag)     run the poll loop in the foreground
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

API_URL="" ; API_KEY="" ; NAME="$(hostname)" ; POLL="5" ; MODE="foreground"
while [ $# -gt 0 ]; do
  case "$1" in
    --api-url) API_URL="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --name)    NAME="$2";    shift 2 ;;
    --poll)    POLL="$2";    shift 2 ;;
    --service) MODE="service"; shift ;;
    --once)    MODE="once";    shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done
[ -n "$API_URL" ] && [ -n "$API_KEY" ] || { echo "ERROR: --api-url and --api-key are required" >&2; exit 2; }

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
say(){ printf '\033[36m[setup-node]\033[0m %s\n' "$*"; }

# sudo wrapper — normally interactive; set CRAWLFAST_SUDO_PASS to run non-interactively (automation
# over SSH). Commands must NOT read stdin themselves (we feed the password there).
_sudo(){
  if [ -n "${CRAWLFAST_SUDO_PASS:-}" ]; then printf '%s\n' "$CRAWLFAST_SUDO_PASS" | sudo -S -p '' "$@"
  else sudo "$@"; fi
}

# 1. Prerequisites — only install what's missing.
install_pkgs(){
  local pkgs="$*"
  if command -v apt-get >/dev/null 2>&1; then _sudo apt-get update -qq && _sudo apt-get install -y -qq $pkgs
  elif command -v dnf >/dev/null 2>&1; then _sudo dnf install -y -q $pkgs
  elif command -v brew >/dev/null 2>&1; then brew install $pkgs
  else echo "WARN: no known package manager; install manually: $pkgs" >&2; fi
}
command -v python3 >/dev/null 2>&1 || { say "installing python3"; install_pkgs python3; }

# 2. Python deps — reuse system libs if already importable (fast path for provisioned boxes),
#    else create a venv, else pip --user. PYBIN is what the worker runs with.
PYBIN="python3"
if python3 -c "import requests, yaml" >/dev/null 2>&1; then
  say "python deps already present (system) — using system python3"
else
  say "installing python deps"
  if python3 -m venv "$REPO_DIR/.venv" >/dev/null 2>&1 && "$REPO_DIR/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
    "$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"; PYBIN="$REPO_DIR/.venv/bin/python"
  else
    command -v pip3 >/dev/null 2>&1 || install_pkgs python3-pip python3-venv
    if python3 -m venv "$REPO_DIR/.venv" >/dev/null 2>&1; then
      "$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"; PYBIN="$REPO_DIR/.venv/bin/python"
    else
      python3 -m pip install --user -q -r "$REPO_DIR/requirements.txt"
    fi
  fi
fi

# 3. Config file (worker reads api_base_url + api_key + name; nothing else).
CFG="$REPO_DIR/config.yaml"
cat > "$CFG" <<YAML
api_base_url: "$API_URL"
api_key: "$API_KEY"
worker_name: "$NAME"
poll_interval_seconds: $POLL
YAML
chmod 600 "$CFG"
say "wrote $CFG (node: $NAME → $API_URL)"

run_worker(){ cd "$REPO_DIR" && exec "$PYBIN" -m crawlfast_external_worker.worker "$@"; }

case "$MODE" in
  once)       say "running one cycle"; run_worker --once ;;
  foreground) say "running poll loop (Ctrl-C to stop)"; run_worker ;;
  service)
    UNIT=/etc/systemd/system/crawlfast-worker.service
    say "installing systemd service $UNIT"
    TMP="$(mktemp)"
    cat > "$TMP" <<UNITEOF
[Unit]
Description=crawlfast external worker ($NAME)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$REPO_DIR
ExecStart=$PYBIN -m crawlfast_external_worker.worker
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
    _sudo cp "$TMP" "$UNIT" && rm -f "$TMP"   # temp-file + cp so sudo's stdin stays free for the password
    _sudo systemctl daemon-reload
    _sudo systemctl enable --now crawlfast-worker.service
    say "service enabled + started. Logs: journalctl -u crawlfast-worker -f"
    ;;
esac
