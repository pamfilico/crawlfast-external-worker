"""Thin HTTP client for the crawlfast external-worker API.

Every call sends the ``X-Worker-Api-Key`` header. Responses are the standard envelope
``{"error", "ui_message", "status_code", "data"}``; we return the ``data`` payload.
"""

from __future__ import annotations

import os
import random

import requests


class WorkerApiError(Exception):
    pass


def _maybe_inject_page_fault():
    """Fault injection for stress-testing the page spool. When CRAWLFAST_WORKER_FAIL_PCT is set
    (0-100), submit_page raises a timeout-like error that fraction of the time BEFORE the real POST,
    so the retry+disk-spool+flush recovery path is actually exercised under failure instead of only
    on paper. The message is a TRANSIENT signature (no 'not assigned'/'not found'/'404'), so the
    spool keeps the page and retries it — proving pages are recovered, not lost. Off by default."""
    try:
        pct = float(os.getenv("CRAWLFAST_WORKER_FAIL_PCT", "0") or 0)
    except ValueError:
        pct = 0.0
    if pct > 0 and random.random() * 100 < pct:
        raise WorkerApiError("request to /page failed: simulated POST read timed out (fault injection)")


class CrawlfastWorkerClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"X-Worker-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _send(self, method: str, path: str, auth: bool = True, **kw):
        """One place for every HTTP call — wraps connection/timeout errors + HTTP/envelope errors
        as WorkerApiError, and returns the envelope's ``data``."""
        url = f"{self.base}{path}"
        headers = self._headers() if auth else None
        try:
            resp = requests.request(method, url, headers=headers, timeout=self.timeout, **kw)
        except requests.exceptions.RequestException as exc:  # connection dropped, timeout, DNS, …
            raise WorkerApiError(f"request to {path} failed: {exc}")
        try:
            body = resp.json()
        except ValueError:
            raise WorkerApiError(f"non-JSON response ({resp.status_code}): {resp.text[:200]}")
        if resp.status_code >= 400 or body.get("error"):
            raise WorkerApiError(f"{resp.status_code}: {body.get('ui_message') or body}")
        return body.get("data")

    def health(self) -> dict:
        """Unauthenticated liveness of the worker endpoint group."""
        return self._send("GET", "/api/v1/external-worker/health", auth=False)

    def heartbeat(self, worker_version: str = "", capabilities: list | None = None) -> dict:
        payload = {"worker_version": worker_version}
        if capabilities is not None:
            payload["capabilities"] = capabilities
        return self._send("POST", "/api/v1/external-worker/heartbeat", json=payload)

    def claim_task(self) -> dict | None:
        data = self._send("POST", "/api/v1/external-worker/tasks/claim", json={})
        return (data or {}).get("task")

    def report_progress(self, task_id: str, done: int, total: int, current_url: str = None,
                        title: str = None) -> None:
        """Best-effort incremental progress ping (drives the live monitor). Never raises."""
        try:
            requests.post(
                f"{self.base}/api/v1/external-worker/tasks/{task_id}/progress",
                json={"done": done, "total": total, "current_url": current_url, "title": title},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except Exception:  # noqa: BLE001 — progress is advisory, never fail the crawl over it
            pass

    def submit_page(self, task_id: str, page: dict) -> dict:
        """Ship ONE crawled page's full HTML back to the server, which saves it to S3 + DB (the node
        owns no storage). Called per page during a crawl_all. Raises WorkerApiError on failure so the
        caller can count/log it — a page reported crawled but not saved is the exact bug this fixes."""
        _maybe_inject_page_fault()
        return self._send(
            "POST", f"/api/v1/external-worker/tasks/{task_id}/page",
            json={"page": page},
        )

    def submit_result(self, task_id: str, status: str, result=None, error: str = None) -> dict:
        return self._send(
            "POST", f"/api/v1/external-worker/tasks/{task_id}/result",
            json={"status": status, "result": result, "error": error},
        )
