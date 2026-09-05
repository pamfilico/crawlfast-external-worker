"""The wire contract between a node and crawlfast.

This is the file to read when asking "is the fleet still going to work". The nodes are laptops and
Raspberry Pis on other people's networks; the server cannot reach them, and a protocol change that
looks harmless from the server side (renaming a key, wrapping a body differently, dropping the
envelope on one route) does not fail loudly — the node keeps heartbeating and quietly claims
nothing. That is the "online but running 0" zombie in NODE-OPS.md.

So every field the shipped client actually READS is asserted here, by name, with the code that
reads it cited. The mirror of this file lives in crawlfast-backend
(tests/contract/test_external_worker_protocol.py) and asserts the SERVER still sends them. Two
suites, one contract; either side can break it and one of them will say so.
"""

from __future__ import annotations

import pytest

from crawlfast_external_worker.client import CrawlfastWorkerClient, WorkerApiError


# ── the envelope itself ──────────────────────────────────────────────────────────────────────
def test_every_call_unwraps_the_standard_response_envelope(client, api):
    """`_send` returns `body["data"]`, not the body. Every caller below depends on that."""
    health = client.health()
    assert health["status"] == "ok"
    assert "features" in health, "health must advertise `features`; _compression_available reads it"


def test_an_error_true_body_raises_even_on_http_200(client, api):
    """`_send` checks `body.get("error")` as well as the status code.

    A 200 carrying `error: true` is how flask-core renders some failures, and treating it as
    success would make the worker report a page as saved that the server refused.
    """
    api.fail_next["/api/v1/external-worker/heartbeat"] = 1
    with pytest.raises(WorkerApiError):
        client.heartbeat()


def test_a_non_json_response_is_an_api_error_not_a_crash(client, api, monkeypatch):
    """A proxy/HTML error page must not raise ValueError out of the poll loop."""
    import requests

    class FakeResp:
        status_code = 502
        text = "<html>bad gateway</html>"

        def json(self):
            raise ValueError("no json")

    monkeypatch.setattr(requests, "request", lambda *a, **kw: FakeResp())
    with pytest.raises(WorkerApiError) as exc:
        client.heartbeat()
    assert "non-JSON" in str(exc.value)


# ── per-route keys the worker reads ──────────────────────────────────────────────────────────
def test_heartbeat_returns_the_worker_name(client, api):
    """worker.heartbeat() reads `data["worker"]["name"]` to log its identity."""
    body = client.heartbeat(worker_version="9.9.9", capabilities=["crawl_all"])
    assert body["worker"]["name"] == api.worker_name
    sent = api.heartbeats[-1]
    assert sent["worker_version"] == "9.9.9"
    assert sent["capabilities"] == ["crawl_all"], (
        "capabilities must be sent — the server uses them to decide what to hand this node"
    )


def test_claim_reads_data_task_and_none_means_idle(client, api):
    """`claim_task` returns `data["task"]`. An idle queue must be None, not an exception:
    the poll loop treats anything else as work and would spin."""
    assert client.claim_task() is None
    api.queue({"id": "t1", "task_type": "crawl_all", "payload": {"url": "http://x/"}})
    task = client.claim_task()
    assert task["id"] == "t1" and task["task_type"] == "crawl_all"


def test_submit_page_verdict_is_the_servers_saved_flag(client, api):
    """PageSpool believes `data["saved"]`, not the HTTP status.

    Counting HTTP 200 as saved is the exact bug that produced tasks reporting seven pages crawled
    and one saved: the server accepted the request and then refused the content.
    """
    body = client.submit_page("t1", {"url": "http://x/", "html": "<html></html>"})
    assert body["saved"] is True

    api.reject_pages = True
    body = client.submit_page("t1", {"url": "http://x/blocked", "html": "<html>403</html>"})
    assert body["saved"] is False
    assert body["reason"], "a refusal must say why — it is what the health sweep reports"


def test_submit_result_sends_status_result_and_error(client, api):
    client.submit_result("t1", status="succeeded", result={"pages_saved": 3})
    sent = api.results[-1]
    assert sent["status"] == "succeeded"
    assert sent["result"] == {"pages_saved": 3}
    assert "error" in sent


def test_progress_is_advisory_and_never_raises(client, api):
    """A failing progress ping must not fail the crawl — it only drives a live monitor."""
    api.fail_next["/api/v1/external-worker/tasks/t1/progress"] = 5
    client.report_progress("t1", done=1, total=5, current_url="http://x/")  # must not raise


# ── the routes, by exact path ────────────────────────────────────────────────────────────────
def test_the_paths_are_exactly_these(client, api):
    """Pinned literally. These strings are baked into every node already in the field; a node
    cannot be redeployed by the server, so a renamed route strands the fleet until someone
    physically visits each machine."""
    client.health()
    client.heartbeat()
    client.claim_task()
    client.submit_page("t1", {"url": "u", "html": "<html></html>"})
    client.submit_result("t1", status="succeeded", result={})
    paths = {path for _method, path in api.requests}
    assert {
        "/api/v1/external-worker/health",
        "/api/v1/external-worker/heartbeat",
        "/api/v1/external-worker/tasks/claim",
        "/api/v1/external-worker/tasks/t1/page",
        "/api/v1/external-worker/tasks/t1/result",
    } <= paths


def test_the_api_key_travels_in_x_worker_api_key(client, api):
    client.heartbeat()
    assert api.headers_seen[-1].get("X-Worker-Api-Key") == "test-key"


def test_health_needs_no_key(cfg, api):
    """A node must be able to ping before it has been given a key at all."""
    unkeyed = CrawlfastWorkerClient(cfg.base, api_key="")
    assert unkeyed.health()["status"] == "ok"


# ── optional transports: discovered, never assumed ───────────────────────────────────────────
def test_compression_is_used_only_when_the_server_advertises_it(cfg, api):
    """`compress_pages` is opt-in on the node AND gated on the server's `features`, so a worker
    can roll out ahead of a backend deploy."""
    api.features = []
    client = CrawlfastWorkerClient(cfg.base, cfg.api_key, compress_pages=True)
    client.submit_page("t1", {"url": "u", "html": "<html></html>"})
    assert not any(p.endswith("/page-compressed") for _m, p in api.requests)

    api.features = ["page-compressed"]
    client = CrawlfastWorkerClient(cfg.base, cfg.api_key, compress_pages=True)
    client.submit_page("t1", {"url": "u", "html": "<html></html>"})
    assert any(p.endswith("/page-compressed") for _m, p in api.requests)


def test_a_missing_compressed_route_falls_back_permanently_and_loses_no_page(cfg, api):
    """An older backend answers 404 on /page-compressed. The page must still land, and the worker
    must stop asking for the life of the process rather than paying a 404 per page."""
    api.features = ["page-compressed"]
    api.missing = {"/api/v1/external-worker/tasks/t1/page-compressed"}
    client = CrawlfastWorkerClient(cfg.base, cfg.api_key, compress_pages=True)

    body = client.submit_page("t1", {"url": "u1", "html": "<html>1</html>"})
    assert body["saved"] is True
    client.submit_page("t1", {"url": "u2", "html": "<html>2</html>"})

    compressed = [p for _m, p in api.requests if p.endswith("/page-compressed")]
    assert len(compressed) == 1, "it should have given up after the first 404, not retried per page"
    assert len(api.pages) == 2, "both pages must have landed via the plain route"


def test_the_host_header_override_is_sent(cfg, api):
    """Needed to reach a Caddy vhost by IP — on macOS a `.local` name costs ~5s of mDNS per call."""
    client = CrawlfastWorkerClient(cfg.base, cfg.api_key, host_header="api.crawlfast.local")
    client.heartbeat()
    assert api.headers_seen[-1].get("Host") == "api.crawlfast.local"
