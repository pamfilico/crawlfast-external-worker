"""Per-task result logs on the node — one durable JSONL record per crawled page, filed by client
and lead, so results can be posted back to the server in batches later (offline-friendly).

WHY (2026-08-16): the worker used to log ``saved=1`` for a page the server actually REJECTED (a
403 block page / non-HTML the server won't persist), because it counted the HTTP-200 POST, not the
server's ``{"saved": false}`` body. Two fixes ship together:
  1. the worker now believes the server's ``saved`` flag (see page_spool.submit_with_retry), and
  2. every page outcome is written here — url, saved(true/false), reason, bytes, http status — under
     ``<log_dir>/<client_id>/<lead_id>/<task_id>.jsonl`` (falling back to user_id / website_id when a
     task carries no core ids), plus a ``_summary`` line per task. Nothing is lost if the node is
     offline; ``post_logs.py`` uploads the backlog in batches when you want.

Layout on the node (default ``./logs``, override ``CRAWLFAST_WORKER_LOG_DIR``):

    logs/
      <client_id>/<lead_id>/<task_id>.jsonl     # one line per page + a final _summary line
      <client_id>/<lead_id>/...
      _unfiled/<website_id>/<task_id>.jsonl      # tasks with no client_id/lead_id in the payload

Each line is a self-describing JSON object (has client_id / lead_id / website_id / task_id), so the
batch poster can ship any subset without needing the directory structure.
"""

from __future__ import annotations

import json
import os
import socket
import time

_NODE = socket.gethostname()


def _slug(v) -> str:
    s = str(v) if v not in (None, "") else ""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in s) or "_"


class TaskLogger:
    """Append-only per-task JSONL writer. One instance per worker; call open_task() per task."""

    def __init__(self, log_dir: str | None = None):
        self.dir = log_dir or os.getenv("CRAWLFAST_WORKER_LOG_DIR", "logs")
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, ids: dict) -> str:
        client = _slug(ids.get("client_id"))
        lead = _slug(ids.get("lead_id"))
        if ids.get("client_id") or ids.get("lead_id"):
            sub = os.path.join(client, lead)
        else:
            # No core ids in the payload — file under the identity the node DOES have.
            sub = os.path.join("_unfiled", _slug(ids.get("website_id") or ids.get("user_id")))
        d = os.path.join(self.dir, sub)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{_slug(ids.get('task_id'))}.jsonl")

    @staticmethod
    def ids_from_task(task: dict) -> dict:
        p = task.get("payload") or {}
        return {
            "task_id": task.get("id"),
            "task_type": task.get("task_type"),
            "client_id": p.get("client_id"),
            "lead_id": p.get("lead_id"),
            "website_id": p.get("website_id"),
            "user_id": p.get("user_id"),
            "redis_job_id": p.get("redis_job_id"),
            "start_url": p.get("url"),
        }

    def open_task(self, task: dict) -> "TaskLog":
        return TaskLog(self._path(self.ids_from_task(task)), self.ids_from_task(task))


class TaskLog:
    def __init__(self, path: str, ids: dict):
        self.path = path
        self.ids = ids
        self._fh = open(path, "a", encoding="utf-8")

    def _write(self, obj: dict):
        rec = {"ts": int(time.time()), "node": _NODE, **self.ids, **obj}
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()

    def page(self, url: str, outcome: str, reason: str = None, http_status=None, bytes_=None, title=None):
        """outcome ∈ {'saved','rejected','spooled','error'}."""
        self._write({"kind": "page", "url": url, "outcome": outcome, "reason": reason,
                     "http_status": http_status, "bytes": bytes_, "title": title})

    def summary(self, status: str, saved: int, rejected: int, spooled: int, pages_crawled: int,
                errors: int, elapsed_ms: int):
        self._write({"kind": "_summary", "status": status, "saved": saved, "rejected": rejected,
                     "spooled": spooled, "pages_crawled": pages_crawled, "errors": errors,
                     "elapsed_ms": elapsed_ms})

    def close(self):
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
