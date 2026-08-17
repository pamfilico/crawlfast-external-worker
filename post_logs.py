#!/usr/bin/env python3
"""Push the node's per-task JSONL logs to object storage, then delete the local copy once saved.

The worker writes one JSONL file per crawled task under ``logs/<client>/<lead>/<task>.jsonl``
(see crawlfast_external_worker/task_log.py). This uploads each file's records to the server
(``POST {api}/api/v1/external-worker/logs``, auth ``X-Worker-Api-Key``), which stores them in the
website's object-storage prefix (``<user>/website/<wid>/logs/<task>.jsonl``, public-read) so they
survive the node and are viewable/downloadable from the dashboards. **Only after the server confirms
the task was saved is the local file DELETED** — so nothing is lost if the server is unreachable
(the file just stays for the next run). Idempotent: re-uploading a task overwrites its object.

    python post_logs.py                       # uses config.yaml / env for api_base_url + api_key
    python post_logs.py --url https://api.crawlfa.st
    python post_logs.py --dry-run             # show what WOULD be pushed, touch nothing
    python post_logs.py --keep                # push but DON'T delete (archive to logs/_posted/)
    python post_logs.py --log-dir logs

Meant to run from cron on the node (see push-logs-cron.sh). Offline-friendly — if the server is
down it leaves the logs for next time.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys


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
    ap.add_argument("--log-dir", default=os.getenv("CRAWLFAST_WORKER_LOG_DIR", "logs"))
    ap.add_argument("--archive-dir", default=None, help="with --keep, move pushed files here (default <log-dir>/_posted)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep", action="store_true", help="archive pushed files instead of deleting them")
    a = ap.parse_args(argv)

    archive = a.archive_dir or os.path.join(a.log_dir, "_posted")
    archive_name = os.path.basename(archive.rstrip("/"))

    files = sorted(iter_log_files(a.log_dir, archive_name))
    if not files:
        print(f"no logs under {a.log_dir}")
        return 0

    client = None
    if not a.dry_run:
        try:
            from crawlfast_external_worker.client import CrawlfastWorkerClient
            from crawlfast_external_worker.config import load_config
            cfg = load_config(a.config)
            base = a.url or cfg.base
            client = CrawlfastWorkerClient(base, cfg.api_key, timeout=cfg.request_timeout_seconds)
        except Exception as e:  # noqa: BLE001
            print(f"could not build client ({e}); falling back to --dry-run")
            a.dry_run = True

    pushed_tasks = pushed_records = removed = kept = failed = 0
    for fp in files:
        recs = read_records(fp)
        if not recs:
            # empty/garbage file — remove it so it doesn't linger
            if not a.dry_run and not a.keep:
                try:
                    os.remove(fp)
                    removed += 1
                except OSError:
                    pass
            continue
        if a.dry_run:
            print(f"[dry-run] would push {len(recs)} record(s) from {os.path.relpath(fp, a.log_dir)}")
            continue
        try:
            # _send already unwraps the standard_response envelope and returns the ``data`` payload.
            data = client._send("POST", "/api/v1/external-worker/logs", json={"records": recs}) or {}  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            print(f"push failed for {os.path.relpath(fp, a.log_dir)} ({e}); leaving in place")
            failed += 1
            continue
        saved = data.get("saved") or []
        if not saved:
            # server accepted the request but stored nothing (e.g. website not resolvable) — keep it.
            print(f"not stored (skipped={data.get('skipped')}) for {os.path.relpath(fp, a.log_dir)}; leaving in place")
            failed += 1
            continue
        pushed_tasks += len(saved)
        pushed_records += sum(int(s.get("records", 0)) for s in saved)
        # confirmed saved → delete (or archive with --keep)
        if a.keep:
            os.makedirs(archive, exist_ok=True)
            dest = os.path.join(archive, os.path.relpath(fp, a.log_dir))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(fp, dest)
            kept += 1
        else:
            try:
                os.remove(fp)
                removed += 1
            except OSError as e:
                print(f"saved but could not delete {fp}: {e}")

    verb = "would push" if a.dry_run else "pushed"
    print(f"{verb} {pushed_tasks} task(s) / {pushed_records} record(s); "
          f"deleted {removed}, archived {kept}, left {failed} for retry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
