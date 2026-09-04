"""Thin HTTP client for the crawlfast external-worker API.

Every call sends the ``X-Worker-Api-Key`` header. Responses are the standard envelope
``{"error", "ui_message", "status_code", "data"}``; we return the ``data`` payload.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import random

import requests

log = logging.getLogger("crawlfast-worker")


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


#: Server feature name for the gzipped page route (advertised on /health).
FEATURE_COMPRESSED_PAGES = "page-compressed"


class CrawlfastWorkerClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0,
                 use_session: bool = False, compress_pages: bool = False, host_header: str = ""):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.host_header = host_header or ""
        #: One pooled connection for every call instead of a fresh TCP+TLS handshake per request.
        #: Opt-in: a node that does not set it behaves exactly as it always has. Measured on a
        #: live node, an API call costs 255-400ms cold and 67ms warm.
        self.session = requests.Session() if use_session else None
        self.compress_pages = compress_pages
        #: None = not asked yet. Resolved once from /health so the worker DISCOVERS the transport
        #: instead of assuming a server version; pinned to False if the route turns out not to
        #: exist, so an older backend costs exactly one wasted request.
        self._server_supports_compression = None

    def _headers(self) -> dict:
        h = {"X-Worker-Api-Key": self.api_key, "Content-Type": "application/json"}
        if self.host_header:
            h["Host"] = self.host_header
        return h

    def _request(self, method, url, **kw):
        """Pooled session when enabled, module-level requests otherwise (original behaviour)."""
        if self.session is not None:
            return self.session.request(method, url, **kw)
        return requests.request(method, url, **kw)

    def _send(self, method: str, path: str, auth: bool = True, **kw):
        """One place for every HTTP call — wraps connection/timeout errors + HTTP/envelope errors
        as WorkerApiError, and returns the envelope's ``data``."""
        url = f"{self.base}{path}"
        headers = self._headers() if auth else ({"Host": self.host_header} if self.host_header else None)
        try:
            resp = self._request(method, url, headers=headers, timeout=self.timeout, **kw)
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
            self._request(
                "POST",
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
        if not self._compression_available():
            return self._send("POST", f"/api/v1/external-worker/tasks/{task_id}/page",
                              json={"page": page})

        body = gzip.compress(json.dumps({"page": page}).encode("utf-8"), 6)
        headers = {**self._headers(), "Content-Encoding": "gzip"}
        url = f"{self.base}/api/v1/external-worker/tasks/{task_id}/page-compressed"
        try:
            resp = self._request("POST", url, data=body, headers=headers, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise WorkerApiError(f"request to /page-compressed failed: {exc}")
        # Route missing on an older backend: stop trying for the life of the process and fall
        # through to the route that has always existed. Never lose a page over a transport choice.
        if resp.status_code in (404, 405):
            log.warning("server has no /page-compressed route; falling back to /page permanently")
            self._server_supports_compression = False
            return self._send("POST", f"/api/v1/external-worker/tasks/{task_id}/page",
                              json={"page": page})
        try:
            payload = resp.json()
        except ValueError:
            raise WorkerApiError(f"non-JSON response ({resp.status_code}): {resp.text[:200]}")
        if resp.status_code >= 400 or payload.get("error"):
            raise WorkerApiError(f"{resp.status_code}: {payload.get('ui_message') or payload}")
        return payload.get("data")

    def _compression_available(self) -> bool:
        """Whether to gzip page bodies: the flag is on AND the server understands them.

        Asked once, from /health. Discovery beats a version check because the answer comes from
        the server that will actually receive the body — so a worker can roll out ahead of a
        backend deploy and simply keep using the old route until the new one appears.
        """
        if not self.compress_pages:
            return False
        if self._server_supports_compression is None:
            try:
                features = (self.health() or {}).get("features") or []
            except Exception:  # noqa: BLE001 — health is unauthenticated; a blip must not
                # permanently disable compression, so leave unresolved and re-ask on the next page.
                return False
            self._server_supports_compression = FEATURE_COMPRESSED_PAGES in features
            log.info("server compression support: %s", self._server_supports_compression)
        return self._server_supports_compression

    def submit_result(self, task_id: str, status: str, result=None, error: str = None) -> dict:
        return self._send(
            "POST", f"/api/v1/external-worker/tasks/{task_id}/result",
            json={"status": status, "result": result, "error": error},
        )
