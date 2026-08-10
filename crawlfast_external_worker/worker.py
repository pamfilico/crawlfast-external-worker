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


def heartbeat(client: CrawlfastWorkerClient) -> str:
    hb = client.heartbeat(worker_version=__version__, capabilities=supported_task_types())
    return (hb or {}).get("worker", {}).get("name", "?")


def process_one(client: CrawlfastWorkerClient, cfg) -> bool:
    """Claim (at most) one task and run it. Returns True if a task was processed. No heartbeat here —
    the loop heartbeats on its own cadence so draining a queue isn't throttled by heartbeat cost."""
    task = client.claim_task()
    if not task:
        return False

    task_id = task["id"]
    log.info("claimed task %s (%s) payload=%s", task_id, task.get("task_type"), task.get("payload"))

    # Progress pings drive the monitor for MULTI-page tasks. Skip them for single-page tasks
    # (total<=1) — in a distributed crawl every page is its own task, and an extra round-trip per
    # page would just add latency. Throttle the rest to ~1/sec.
    _last = [0.0]

    def on_progress(done, total, current_url=None, title=None):
        if (total or 1) <= 1:
            return
        now = time.time()
        if now - _last[0] < 1.0 and done != total:
            return
        _last[0] = now
        log.info("  progress %s/%s %s", done, total, current_url or "")
        client.report_progress(task_id, done=done, total=total, current_url=current_url, title=title)

    try:
        result = execute(task, cfg, on_progress=on_progress)
        client.submit_result(task_id, status="succeeded", result=result)
        pages = result.get("pages_crawled")
        log.info("task %s succeeded%s", task_id,
                 f" — {pages} pages in {result.get('elapsed_seconds')}s" if pages is not None else "")
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
            log.info("identified as %s", heartbeat(client))
            if not process_one(client, cfg):
                log.info("no pending tasks")
            return 0
        except WorkerApiError as exc:
            log.error("cycle failed: %s", exc)
            return 1

    # Long-running loop. DRAIN greedily: keep claiming back-to-back while there's work (this is what
    # makes a distributed crawl fast — no sleep between pages). Heartbeat only every ~10s, and sleep
    # the poll interval only when the queue is empty.
    log.info("entering poll loop (drain mode, poll every %ss); Ctrl-C to stop", cfg.poll_interval_seconds)
    hb_interval = max(10.0, cfg.poll_interval_seconds)  # heartbeat ~every 10s, never per-task
    last_hb = 0.0
    idle = 0
    while True:
        try:
            now = time.time()
            if now - last_hb >= hb_interval:
                log.info("identified as %s", heartbeat(client))
                last_hb = now
            if process_one(client, cfg):
                idle = 0  # got work — loop straight back to claim the next (no sleep)
            else:
                # Empty right now, but a running job's frontier refills within ~1s as pages get
                # crawled elsewhere — so retry FAST for a while before backing off to poll_interval.
                idle += 1
                time.sleep(0.3 if idle < 20 else cfg.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("stopping")
            return 0
        except Exception as exc:  # noqa: BLE001 — NEVER crash the node: API errors, dropped
            # connections (e.g. a server reload), timeouts — log and keep polling.
            log.warning("transient error, continuing: %s", exc)
            last_hb = 0.0  # force a re-heartbeat next loop
            time.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
