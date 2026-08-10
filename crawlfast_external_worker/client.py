"""Thin HTTP client for the crawlfast external-worker API.

Every call sends the ``X-Worker-Api-Key`` header. Responses are the standard envelope
``{"error", "ui_message", "status_code", "data"}``; we return the ``data`` payload.
"""

from __future__ import annotations

import requests


class WorkerApiError(Exception):
    pass


class CrawlfastWorkerClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"X-Worker-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _data(self, resp: requests.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            raise WorkerApiError(f"non-JSON response ({resp.status_code}): {resp.text[:200]}")
        if resp.status_code >= 400 or body.get("error"):
            raise WorkerApiError(f"{resp.status_code}: {body.get('ui_message') or body}")
        return body.get("data")

    def health(self) -> dict:
        """Unauthenticated liveness of the worker endpoint group."""
        resp = requests.get(f"{self.base}/api/v1/external-worker/health", timeout=self.timeout)
        return self._data(resp)

    def heartbeat(self, worker_version: str = "", capabilities: list | None = None) -> dict:
        payload = {"worker_version": worker_version}
        if capabilities is not None:
            payload["capabilities"] = capabilities
        resp = requests.post(
            f"{self.base}/api/v1/external-worker/heartbeat",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        return self._data(resp)

    def claim_task(self) -> dict | None:
        resp = requests.post(
            f"{self.base}/api/v1/external-worker/tasks/claim",
            json={},
            headers=self._headers(),
            timeout=self.timeout,
        )
        data = self._data(resp)
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

    def submit_result(self, task_id: str, status: str, result=None, error: str = None) -> dict:
        resp = requests.post(
            f"{self.base}/api/v1/external-worker/tasks/{task_id}/result",
            json={"status": status, "result": result, "error": error},
            headers=self._headers(),
            timeout=self.timeout,
        )
        return self._data(resp)
