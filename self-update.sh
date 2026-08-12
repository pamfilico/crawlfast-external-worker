#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# crawlfast worker self-update — run by cron. Pulls the latest code and, if it changed, rebuilds +
# restarts the container. Also re-ups the container if it died (self-heal).
#
# SELF-HEALING: if a pulled commit breaks the BUILD, `docker compose up --build` fails but the OLD
# container keeps running — so the node stays alive on the last-good image. If the new code builds
# but crashes at RUNTIME, `restart: unless-stopped` keeps retrying. Either way, push a fix and the
# NEXT cron run pulls it and recovers. Cron is independent of the worker, so it never gets stuck.
#
# Installed by bootstrap.sh as /etc/cron.d/crawlfast-worker. Runs as the node user (in the docker
# group, so plain `docker` works — no sudo in cron).
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(cd "$(dirname "$0")" && pwd)" || exit 0
log(){ printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

# docker compose, with a sudo fallback if the user isn't in the docker group yet.
dc(){ if docker compose version >/dev/null 2>&1; then docker compose "$@"; else sudo -n docker compose "$@"; fi; }

# Persisted worker count: a plain `docker compose up -d` resets replicas to 1, so every cron run
# would wipe a manual scale-up. Honor a `.worker-replicas` file (or WORKER_REPLICAS env) so the
# chosen count survives self-updates. Default 1. Set it with: echo 8 > .worker-replicas
REPLICAS="${WORKER_REPLICAS:-$(cat .worker-replicas 2>/dev/null || echo 1)}"
case "$REPLICAS" in ''|*[!0-9]*) REPLICAS=1;; esac
SCALE="--scale worker=$REPLICAS"

before="$(git rev-parse HEAD 2>/dev/null || echo none)"
if git pull --ff-only -q 2>/dev/null; then
  after="$(git rev-parse HEAD 2>/dev/null || echo none)"
  if [ "$before" != "$after" ]; then
    log "updated $before -> $after; rebuilding (replicas=$REPLICAS)"
    if dc up -d --build --remove-orphans $SCALE; then
      log "rebuild OK"
    else
      log "REBUILD FAILED — keeping last-good container running; will retry next run"
    fi
  else
    dc up -d $SCALE >/dev/null 2>&1 && log "up-to-date ($after); ensured running (replicas=$REPLICAS)" || log "up-to-date; ensure-running failed"
  fi
else
  log "git pull failed (offline?); ensuring current container is running"
  dc up -d $SCALE >/dev/null 2>&1 || true
fi
