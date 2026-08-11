#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# crawlfast external worker — one-command DOCKER onboarding for a farm node.
#
# Turns a fresh Linux box (laptop, Raspberry Pi, VM) into a running crawl node:
# installs Docker + mDNS (.local), writes config, and `docker compose up -d`. Idempotent — re-run
# to update/reconfigure. Runs on the NODE (usually invoked remotely by the onboard skill).
#
#   ./bootstrap.sh --api-url http://api-box.local:5099 --api-key cfw_XXX --name crawlfast-node2
#   ./bootstrap.sh --uninstall                      # remove the worker (+ old systemd unit)
#
# Flags: --api-url URL  --api-key KEY  --name NAME  [--poll N]  [--scale N]  [--uninstall]
# Non-interactive sudo: export CRAWLFAST_SUDO_PASS=... (for remote/automated runs).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

API_URL="" ; API_KEY="" ; NAME="$(hostname)" ; POLL="5" ; SCALE="1" ; UNINSTALL=0
UPDATE_INTERVAL="15" ; NO_CRON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --api-url) API_URL="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --name)    NAME="$2";    shift 2 ;;
    --poll)    POLL="$2";    shift 2 ;;
    --scale)   SCALE="$2";   shift 2 ;;
    --update-interval) UPDATE_INTERVAL="$2"; shift 2 ;;  # minutes between self-update runs
    --no-cron) NO_CRON=1;    shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
say(){ printf '\033[36m[bootstrap]\033[0m %s\n' "$*"; }
_sudo(){ if [ -n "${CRAWLFAST_SUDO_PASS:-}" ]; then printf '%s\n' "$CRAWLFAST_SUDO_PASS" | sudo -S -p '' "$@"; else sudo "$@"; fi; }
dc(){ _sudo docker compose "$@"; }   # docker needs root until the user's group membership takes effect

if [ "$UNINSTALL" = 1 ]; then
  say "uninstalling worker"
  ( cd "$REPO_DIR" && dc down --remove-orphans ) 2>/dev/null || true   # stop the container(s)
  _sudo rm -f /etc/cron.d/crawlfast-worker 2>/dev/null || true          # remove self-update cron
  # remove the legacy host-python systemd unit if a previous setup-node.sh left one
  _sudo systemctl stop crawlfast-worker.service 2>/dev/null || true
  _sudo systemctl disable crawlfast-worker.service 2>/dev/null || true
  _sudo rm -f /etc/systemd/system/crawlfast-worker.service 2>/dev/null || true
  _sudo systemctl daemon-reload 2>/dev/null || true
  pkill -f crawlfast_external_worker 2>/dev/null || true                # belt-and-suspenders
  say "done. (Docker + avahi left installed.)"
  exit 0
fi

[ -n "$API_URL" ] && [ -n "$API_KEY" ] || { echo "ERROR: --api-url and --api-key are required" >&2; exit 2; }

install_pkgs(){ if command -v apt-get >/dev/null 2>&1; then _sudo apt-get update -qq && _sudo apt-get install -y -qq "$@"
                elif command -v dnf >/dev/null 2>&1; then _sudo dnf install -y -q "$@"; else echo "WARN: install manually: $*" >&2; fi; }

# 1. Base tools
command -v curl >/dev/null 2>&1 || install_pkgs curl ca-certificates
command -v git  >/dev/null 2>&1 || install_pkgs git

# 2. Docker (official convenience script → uniform across distros + arches incl. Raspberry Pi).
if ! command -v docker >/dev/null 2>&1; then
  say "installing Docker"
  curl -fsSL https://get.docker.com | _sudo sh
fi
_sudo systemctl enable --now docker 2>/dev/null || true
_sudo usermod -aG docker "$(id -un)" 2>/dev/null || true   # takes effect next login; we use sudo meanwhile

# 3. mDNS — advertise <name>.local + resolve other .local names. Best-effort.
if command -v apt-get >/dev/null 2>&1 && ! systemctl is-active --quiet avahi-daemon 2>/dev/null; then
  say "installing mDNS (avahi)"
  install_pkgs avahi-daemon avahi-utils libnss-mdns || true
  _sudo systemctl enable --now avahi-daemon 2>/dev/null || true
fi
if [ "$(hostname)" != "$NAME" ] && command -v hostnamectl >/dev/null 2>&1; then
  say "setting hostname → $NAME (advertises $NAME.local)"; _sudo hostnamectl set-hostname "$NAME" || true
fi

# 4. Config (the node knows ONLY this — an API URL + its key).
cat > "$REPO_DIR/config.yaml" <<YAML
api_base_url: "$API_URL"
api_key: "$API_KEY"
worker_name: "$NAME"
poll_interval_seconds: $POLL
YAML
chmod 600 "$REPO_DIR/config.yaml"
say "wrote config.yaml ($NAME → $API_URL)"

# 5. Up (build the tiny image, run detached, restart-on-boot via compose + docker enabled).
cd "$REPO_DIR"
chmod +x self-update.sh 2>/dev/null || true
dc up -d --build --scale worker="$SCALE" --remove-orphans
say "worker up (scale=$SCALE). Logs: sudo docker compose logs -f"

# 6. Self-update cron — every $UPDATE_INTERVAL min: git pull, rebuild if changed, self-heal if the
#    container died. A bad pull can't wedge the node: cron is independent and the next run recovers
#    (a broken BUILD leaves the last-good container running; push a fix → next run picks it up).
if [ "$NO_CRON" != 1 ]; then
  CRON=/etc/cron.d/crawlfast-worker
  say "installing self-update cron (every ${UPDATE_INTERVAL}m) → $CRON"
  TMP="$(mktemp)"
  cat > "$TMP" <<CRONEOF
# crawlfast worker self-update (managed by bootstrap.sh)
PATH=/usr/local/bin:/usr/bin:/bin
*/$UPDATE_INTERVAL * * * * $(id -un) cd $REPO_DIR && ./self-update.sh >> $REPO_DIR/self-update.log 2>&1
CRONEOF
  _sudo cp "$TMP" "$CRON" && _sudo chmod 644 "$CRON" && rm -f "$TMP"
fi

dc ps
