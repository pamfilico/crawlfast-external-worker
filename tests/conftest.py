"""Fixtures for the worker's own suite.

Everything the worker talks to is faked IN-PROCESS over real HTTP on 127.0.0.1:

  * ``site_server``  — a website to crawl (``tests/fixtures/site``), whose per-path behaviour
    (status, content type, delay, headers) the test can rewrite.
  * ``api_server``   — the crawlfast external-worker API, answering the real envelope shape.

Real sockets, real ``requests``, real HTML parsing. The only thing not real is the other end, so
these tests exercise the code that actually ships to the nodes rather than a mock of it.

A guard refuses any connection that is not loopback: the crawler's whole job is to fetch URLs, and
a test suite for it must never be one typo away from crawling the internet.
"""

from __future__ import annotations

import http.server
import socket
import threading
from pathlib import Path

import pytest

FIXTURE_SITE = Path(__file__).parent / "fixtures" / "site"


# ── no test may leave the loopback interface ────────────────────────────────────────────────
_real_create_connection = socket.create_connection
_real_socket_connect = socket.socket.connect

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _check(address):
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, bytes):
        host = host.decode()
    if host not in _ALLOWED_HOSTS:
        raise RuntimeError(
            f"Blocked outbound connection to {host!r}. The worker suite fetches URLs for a living; "
            "every target must be the in-process fixture server, never a real site."
        )


@pytest.fixture(autouse=True, scope="session")
def _loopback_only():
    def create_connection(address, *a, **kw):
        _check(address)
        return _real_create_connection(address, *a, **kw)

    def connect(self, address):
        _check(address)
        return _real_socket_connect(self, address)

    socket.create_connection = create_connection
    socket.socket.connect = connect
    try:
        yield
    finally:
        socket.create_connection = _real_create_connection
        socket.socket.connect = _real_socket_connect


class _ThreadedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _serve(handler_cls) -> tuple[str, _ThreadedServer]:
    server = _ThreadedServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


# ── the website under test ──────────────────────────────────────────────────────────────────
class FakeSite:
    """A static site the test can bend: force a status, a content type, a header, or a hang.

    ``rules`` is keyed by request path. ``hits`` records every path requested, in order, which is
    how the crawl tests assert what was fetched and what was correctly skipped.
    """

    def __init__(self):
        self.rules: dict[str, dict] = {}
        self.hits: list[str] = []
        self.url = ""

    def set(self, path: str, *, status=None, content_type=None, body=None, headers=None):
        self.rules[path] = {
            "status": status, "content_type": content_type, "body": body, "headers": headers or {}
        }

    def reset(self):
        self.rules.clear()
        self.hits.clear()

    def fetched(self, suffix: str) -> int:
        return sum(1 for h in self.hits if h.endswith(suffix))


@pytest.fixture(scope="session")
def _site_state():
    return FakeSite()


@pytest.fixture(scope="session")
def _site_server(_site_state):
    state = _site_state

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(FIXTURE_SITE), **kw)

        def log_message(self, *a):  # keep pytest output readable
            pass

        def do_GET(self):
            state.hits.append(self.path)
            rule = state.rules.get(self.path)
            if rule is None:
                return super().do_GET()
            body = (rule["body"] or "").encode()
            self.send_response(rule["status"] or 200)
            self.send_header("Content-Type", rule["content_type"] or "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in rule["headers"].items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    url, server = _serve(Handler)
    state.url = url
    yield state
    server.shutdown()


@pytest.fixture
def site(_site_server):
    """The fixture website, reset between tests."""
    _site_server.reset()
    return _site_server


# ── the crawlfast external-worker API ───────────────────────────────────────────────────────
#: The envelope every crawlfast route returns. Spelled out here rather than imported, because the
#: point of these tests is to catch the day the SERVER stops sending this shape — importing the
#: server's own helper would make that change invisible. See tests/test_client_protocol.py.
def envelope(data=None, *, error=False, ui_message="", status_code=200):
    return {
        "error": error,
        "ui_message": ui_message,
        "status_code": status_code,
        "redirect_to_login": False,
        "data": data,
    }


class FakeApi:
    """The crawlfast side of the pull protocol, in-process.

    Holds a queue of tasks to hand out, records everything the worker posted back, and lets a test
    script per-route failures (``fail_next``) to prove the retry/spool behaviour.
    """

    def __init__(self):
        self.url = ""
        self.tasks: list[dict] = []
        self.claimed: list[dict] = []
        self.pages: list[dict] = []
        self.results: list[dict] = []
        self.progress: list[dict] = []
        self.heartbeats: list[dict] = []
        self.requests: list[tuple[str, str]] = []      # (method, path)
        self.headers_seen: list[dict] = []
        self.features = ["page-compressed"]
        #: path -> number of times to fail before succeeding
        self.fail_next: dict[str, int] = {}
        #: paths to answer 404 for, to simulate an older backend
        self.missing: set[str] = set()
        self.reject_pages = False
        self.worker_name = "test-node"

    def reset(self):
        self.__init__()

    def queue(self, task: dict):
        self.tasks.append(task)


@pytest.fixture(scope="session")
def _api_state():
    return FakeApi()


@pytest.fixture(scope="session")
def _api_server(_api_state):
    import gzip
    import json as _json

    state = _api_state

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # -- helpers ----------------------------------------------------------------------
        def _send(self, payload, status=200):
            body = _json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if (self.headers.get("Content-Encoding") or "") == "gzip":
                raw = gzip.decompress(raw)
            return _json.loads(raw or b"{}")

        def _should_fail(self) -> bool:
            left = state.fail_next.get(self.path, 0)
            if left > 0:
                state.fail_next[self.path] = left - 1
                return True
            return False

        # -- routes -----------------------------------------------------------------------
        def do_GET(self):
            state.requests.append(("GET", self.path))
            if self.path == "/api/v1/external-worker/health":
                return self._send(envelope({
                    "status": "ok", "service": "external-worker", "features": state.features,
                    "server_time": "2026-09-05T00:00:00+00:00",
                }))
            self._send(envelope(None, error=True, ui_message="not found", status_code=404), 404)

        def do_POST(self):
            state.requests.append(("POST", self.path))
            state.headers_seen.append(dict(self.headers))
            if self.path in state.missing:
                return self._send(envelope(None, error=True, ui_message="no route",
                                           status_code=404), 404)
            if self._should_fail():
                return self._send(envelope(None, error=True, ui_message="boom",
                                           status_code=500), 500)

            path = self.path
            if path == "/api/v1/external-worker/heartbeat":
                state.heartbeats.append(self._body())
                return self._send(envelope({
                    "worker": {"name": state.worker_name, "id": "w1"},
                    "poll_interval_seconds": 5,
                }))
            if path == "/api/v1/external-worker/tasks/claim":
                task = state.tasks.pop(0) if state.tasks else None
                if task:
                    state.claimed.append(task)
                return self._send(envelope({"task": task}))
            if path.endswith("/progress"):
                state.progress.append(self._body())
                return self._send(envelope({"ok": True}))
            if path.endswith("/page") or path.endswith("/page-compressed"):
                body = self._body()
                state.pages.append(body.get("page") or {})
                if state.reject_pages:
                    return self._send(envelope({"saved": False, "reason": "blocked-page"}))
                return self._send(envelope({"saved": True, "page_id": f"p{len(state.pages)}"}))
            if path.endswith("/result"):
                state.results.append(self._body())
                return self._send(envelope({"ok": True}))
            if path == "/api/v1/external-worker/logs":
                return self._send(envelope({"ok": True}))
            self._send(envelope(None, error=True, ui_message="not found", status_code=404), 404)

    url, server = _serve(Handler)
    state.url = url
    yield state
    server.shutdown()


@pytest.fixture
def api(_api_server):
    """The fake crawlfast API, reset between tests."""
    url = _api_server.url
    _api_server.reset()
    _api_server.url = url
    return _api_server


@pytest.fixture
def cfg(api):
    """A WorkerConfig pointed at the fake API, with every opt-in transport flag OFF (the default a
    deployed node runs with)."""
    from crawlfast_external_worker.config import WorkerConfig

    return WorkerConfig(api_base_url=api.url, api_key="test-key", worker_name="test-node",
                        poll_interval_seconds=0.01, request_timeout_seconds=5.0)


@pytest.fixture
def client(cfg):
    from crawlfast_external_worker.client import CrawlfastWorkerClient

    return CrawlfastWorkerClient(cfg.base, cfg.api_key, timeout=cfg.request_timeout_seconds)
