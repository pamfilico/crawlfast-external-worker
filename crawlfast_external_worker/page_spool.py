"""Local page spool — never silently lose a crawled page to a server timeout.

The worker POSTs each crawled page's HTML to the server (which saves it to S3 + DB). When that POST
fails (the server is slow/restarting → read/write timeout), the page would otherwise be dropped and
the task would still "succeed" with pages missing. Instead we:

  1. retry the POST a few times with backoff (handles a brief blip), and
  2. if it still fails, write the page to a local spool directory on disk;
  3. flush the spool periodically (and on the next run) — re-POSTing buffered pages until they land.

The spool is plain JSON files on disk, so pages survive across worker restarts. Bounded flushes keep
it from blocking the crawl.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

log = logging.getLogger("crawlfast-worker")


class PageSpool:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("CRAWLFAST_WORKER_SPOOL", ".page-spool")
        os.makedirs(self.path, exist_ok=True)

    def _spool(self, task_id: str, page: dict) -> str:
        fn = os.path.join(self.path, f"{uuid.uuid4().hex}.json")
        tmp = fn + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"task_id": task_id, "page": page}, f)
        os.replace(tmp, fn)  # atomic — a crash mid-write can't leave a half file in the queue
        return fn

    def pending(self):
        try:
            return [os.path.join(self.path, f) for f in os.listdir(self.path) if f.endswith(".json")]
        except FileNotFoundError:
            return []

    def submit_with_retry(self, client, task_id: str, page: dict, retries: int = 3) -> bool:
        """POST a page, retrying with backoff; on final failure, spool it to disk. Returns True only
        when the server accepted it."""
        delay = 0.5
        for attempt in range(retries):
            try:
                client.submit_page(task_id, page)
                return True
            except Exception as exc:  # noqa: BLE001
                if attempt == retries - 1:
                    self._spool(task_id, page)
                    log.warning("  page save FAILED (spooled for retry) for %s: %s",
                                page.get("url"), exc)
                    return False
                time.sleep(delay)
                delay *= 2
        return False

    def flush(self, client, max_files: int = 100):
        """Re-POST buffered pages. Delete on success, or when the server permanently rejects the page
        (task gone/reassigned). Leave transient failures for the next flush. Bounded per call."""
        files = self.pending()[:max_files]
        recovered = dropped = 0
        for fn in files:
            try:
                with open(fn, encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:  # noqa: BLE001 — corrupt file, drop it
                _safe_remove(fn)
                continue
            try:
                client.submit_page(rec.get("task_id"), rec.get("page") or {})
                _safe_remove(fn)
                recovered += 1
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if "not assigned" in msg or "not found" in msg or "404" in msg:
                    _safe_remove(fn)  # task gone/reassigned — this worker can't land it anymore
                    dropped += 1
                # else: transient (timeout again) — keep it for the next flush
        if recovered or dropped:
            log.info("  spool flush: recovered=%d dropped=%d remaining=%d",
                     recovered, dropped, len(self.pending()))
        return recovered, dropped


def _safe_remove(fn):
    try:
        os.remove(fn)
    except OSError:
        pass
