#!/usr/bin/env python3
"""Post the node's per-task JSONL logs to the server in batches, then archive them.

The worker writes one JSONL record per crawled page under ``logs/<client>/<lead>/<task>.jsonl``
(see crawlfast_external_worker/task_log.py). This uploads that backlog in batches so results can be
reconciled centrally, then moves the uploaded files to ``logs/_posted/`` so they aren't sent twice.
Offline-friendly: run it from cron; if the server is down it just leaves the logs for next time.

    python post_logs.py                       # uses config.yaml / env for api_base_url + api_key
    python post_logs.py --url https://api.crawlfa.st --batch 500
    python post_logs.py --dry-run             # show what WOULD be posted, touch nothing
    python post_logs.py --log-dir logs --archive-dir logs/_posted

Endpoint (server-side, to be added): POST {api}/api/v1/external-worker/logs  body {"records": [...]}
auth ``X-Worker-Api-Key``. Until it exists, use --dry-run (default if no url/config is resolvable).
Each record is self-describing (carries client_id/lead_id/website_id/task_id/url/outcome), so the
server can file them without the directory structure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

# client/config imported lazily inside main() so --dry-run works without `requests` installed


def iter_log_files(log_dir: str, archive_name: str):
    for root, _dirs, files in os.walk(log_dir):
        if archive_name in root.split(os.sep):
            continue
        for fn in files:
            if fn.endswith(".jsonl"):
                yield os.path.join(root, fn)


def read_records(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--url", default=None, help="API base (else config.yaml / CRAWLFAST_WORKER_API_BASE_URL)")
    ap.add_argument("--batch", type=int, default=500, help="records per POST")
    ap.add_argument("--log-dir", default=os.getenv("CRAWLFAST_WORKER_LOG_DIR", "logs"))
    ap.add_argument("--archive-dir", default=None, help="move posted files here (default <log-dir>/_posted)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    archive = a.archive_dir or os.path.join(a.log_dir, "_posted")
    archive_name = os.path.basename(archive.rstrip("/"))

    files = sorted(iter_log_files(a.log_dir, archive_name))
    if not files:
        print(f"no logs under {a.log_dir}")
        return 0

    # Gather (file, records); post in batches; archive files once all their records are accepted.
    total = 0
    client = None
    if not a.dry_run:
        try:
            from crawlfast_external_worker.config import load_config
            from crawlfast_external_worker.client import CrawlfastWorkerClient
            cfg = load_config(a.config)
            base = (a.url or cfg.base)
            client = CrawlfastWorkerClient(base, cfg.api_key, timeout=cfg.request_timeout_seconds)
        except Exception as e:  # noqa: BLE001
            print(f"could not build client ({e}); falling back to --dry-run")
            a.dry_run = True

    batch: list[dict] = []
    batch_files: set[str] = set()
    pending_files: list[str] = []

    def flush() -> bool:
        nonlocal batch, batch_files, total
        if not batch:
            return True
        if a.dry_run:
            print(f"[dry-run] would POST {len(batch)} record(s) from {len(batch_files)} file(s)")
        else:
            try:
                client._send("POST", "/api/v1/external-worker/logs", json={"records": batch})  # noqa: SLF001
            except Exception as e:  # noqa: BLE001
                print(f"POST failed ({e}); leaving logs in place")
                return False
            os.makedirs(archive, exist_ok=True)
            for fp in batch_files:
                rel = os.path.relpath(fp, a.log_dir)
                dest = os.path.join(archive, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(fp, dest)
        total += len(batch)
        batch, batch_files = [], set()
        return True

    for fp in files:
        recs = read_records(fp)
        for r in recs:
            batch.append(r)
            batch_files.add(fp)
            if len(batch) >= a.batch:
                if not flush():
                    return 1
    if not flush():
        return 1

    print(f"{'[dry-run] ' if a.dry_run else ''}posted {total} record(s) from {len(files)} file(s)"
          + ("" if a.dry_run else f"; archived to {archive}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
