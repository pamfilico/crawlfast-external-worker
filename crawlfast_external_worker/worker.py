"""crawlfast external worker — main loop.

PULL-BASED. On each cycle the node:
  1. heartbeats (so the server knows it's alive — the server can't reach us),
  2. claims one task if any is pending,
  3. executes it (see executor.py) and reports the result.

Two run modes:
  - loop  (default): run forever, sleeping ``poll_interval_seconds`` between cycles.
  - --once          : run a single cycle and exit — ideal for cron (`* * * * * … --once`).

Usage:
    python -m crawlfast_external_worker.worker [--config config.yaml] [--once] [--health]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import __version__
from .client import CrawlfastWorkerClient, WorkerApiError
from .config import load_config
from .executor import execute, supported_task_types

log = logging.getLogger("crawlfast-worker")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def run_cycle(client: CrawlfastWorkerClient, cfg) -> bool:
    """One heartbeat + (at most) one task. Returns True if a task was processed."""
    hb = client.heartbeat(worker_version=__version__, capabilities=supported_task_types())
    worker_name = (hb or {}).get("worker", {}).get("name", "?")
    log.info("heartbeat ok — identified as %s", worker_name)

    task = client.claim_task()
    if not task:
        log.info("no pending tasks")
        return False

    task_id = task["id"]
    log.info("claimed task %s (%s) payload=%s", task_id, task.get("task_type"), task.get("payload"))
    try:
        result = execute(task, cfg)
        client.submit_result(task_id, status="succeeded", result=result)
        log.info("task %s succeeded", task_id)
    except Exception as exc:  # noqa: BLE001 — any failure is reported, never crashes the loop
        log.exception("task %s failed", task_id)
        client.submit_result(task_id, status="failed", error=str(exc))
    return True


def main(argv=None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description="crawlfast external worker")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit (cron mode)")
    parser.add_argument("--health", action="store_true", help="ping the API health endpoint and exit")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    client = CrawlfastWorkerClient(cfg.base, cfg.api_key, timeout=cfg.request_timeout_seconds)
    log.info("crawlfast-external-worker v%s → %s", __version__, cfg.base)

    if args.health:
        try:
            health = client.health()
            log.info("health: %s", health)
            return 0
        except WorkerApiError as exc:
            log.error("health check failed: %s", exc)
            return 1

    if args.once:
        try:
            run_cycle(client, cfg)
            return 0
        except WorkerApiError as exc:
            log.error("cycle failed: %s", exc)
            return 1

    # long-running loop
    log.info("entering poll loop (every %ss); Ctrl-C to stop", cfg.poll_interval_seconds)
    while True:
        try:
            processed = run_cycle(client, cfg)
        except WorkerApiError as exc:
            log.error("api error: %s", exc)
            processed = False
        except KeyboardInterrupt:
            log.info("stopping")
            return 0
        # If we just did work, poll again quickly; if idle, wait the full interval.
        time.sleep(0.5 if processed else cfg.poll_interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
