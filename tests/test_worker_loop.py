"""The poll loop: claim, run, report — and the failure modes that make a node look alive.

The fleet's characteristic failure is not a crash. It is a node that heartbeats forever while
claiming nothing ("online but running 0" in NODE-OPS.md): the loop swallows every error so a blip
never kills it, which also means a sustained failure is invisible. That is why the watchdog exists
and why it is tested here.
"""

from __future__ import annotations

import pytest

from crawlfast_external_worker import worker as worker_mod
from crawlfast_external_worker.page_spool import PageSpool
from crawlfast_external_worker.task_log import TaskLogger


@pytest.fixture
def spool(tmp_path):
    return PageSpool(str(tmp_path / "spool"))


@pytest.fixture
def tlog(tmp_path):
    return TaskLogger(str(tmp_path / "logs"))


def _crawl_task(site, task_id="t1"):
    return {"id": task_id, "task_type": "crawl_all",
            "payload": {"url": f"{site.url}/index.html", "max_pages": 4}}


# ── one cycle ────────────────────────────────────────────────────────────────────────────────
def test_an_empty_queue_processes_nothing(client, cfg, spool, tlog, api):
    assert worker_mod.process_one(client, cfg, spool, tlog) is False
    assert api.results == []


def test_a_claimed_task_is_crawled_and_reported_succeeded(client, cfg, spool, tlog, api, site):
    api.queue(_crawl_task(site))
    assert worker_mod.process_one(client, cfg, spool, tlog) is True

    reported = api.results[-1]
    assert reported["status"] == "succeeded"
    result = reported["result"]
    assert result["pages_crawled"] >= 3
    assert result["pages_saved"] == result["pages_crawled"]
    assert result["pages_rejected"] == 0
    assert result["pages_spooled"] == 0
    assert len(api.pages) == result["pages_saved"], "the server did not receive every page"


def test_the_result_carries_the_servers_verdicts_not_the_workers_guess(
    client, cfg, spool, tlog, api, site
):
    """`pages_saved` must count what the SERVER kept. A task that crawls seven pages and has all
    seven refused must not report seven saved — that is the exact bug CRAWL_HEALTH.md is about."""
    api.reject_pages = True
    api.queue(_crawl_task(site))
    worker_mod.process_one(client, cfg, spool, tlog)

    result = api.results[-1]["result"]
    assert result["pages_saved"] == 0
    assert result["pages_rejected"] == result["pages_crawled"] >= 3
    assert api.results[-1]["status"] == "succeeded", (
        "the crawl itself did not fail — it is the CONTENT that was refused, and the counts are "
        "how that is reported"
    )


def test_a_failing_task_is_reported_failed_not_left_hanging(client, cfg, spool, tlog, api):
    """A task nobody reports on sits claimed until the lease expires, and the work is lost twice."""
    api.queue({"id": "t9", "task_type": "not_a_real_type", "payload": {}})
    assert worker_mod.process_one(client, cfg, spool, tlog) is True
    assert api.results[-1]["status"] == "failed"
    assert "no handler" in api.results[-1]["error"]


def test_pages_that_cannot_be_posted_are_spooled_and_counted(client, cfg, spool, tlog, api, site):
    api.fail_next = {}
    api.missing = {f"/api/v1/external-worker/tasks/t1/page"}
    api.queue(_crawl_task(site))
    worker_mod.process_one(client, cfg, spool, tlog)

    result = api.results[-1]["result"]
    assert result["pages_spooled"] >= 3
    assert result["pages_saved"] == 0
    assert len(spool.pending()) == result["pages_spooled"], "a page was lost instead of spooled"


def test_progress_is_reported_for_a_multi_page_task(client, cfg, spool, tlog, api, site):
    api.queue(_crawl_task(site))
    worker_mod.process_one(client, cfg, spool, tlog)
    assert api.progress, "no progress ping — the live monitor would show nothing for the whole run"
    assert api.progress[-1]["total"] >= 1


def test_a_single_page_task_sends_no_progress(client, cfg, spool, tlog, api, site):
    """In a distributed crawl every page is its own task; a ping per page is a round-trip per page
    for a bar that never moves."""
    api.queue({"id": "t1", "task_type": "crawl_single",
               "payload": {"url": f"{site.url}/about.html"}})
    worker_mod.process_one(client, cfg, spool, tlog)
    assert api.progress == []


def test_every_page_outcome_is_written_to_the_node_log(client, cfg, spool, tlog, api, site):
    """Durable per-page records on the node, so an offline node loses nothing."""
    api.queue(_crawl_task(site))
    worker_mod.process_one(client, cfg, spool, tlog)
    lines = [
        line
        for path in __import__("pathlib").Path(tlog.dir).rglob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert lines, "nothing was logged on the node"
    assert any('"_summary"' in ln or "_summary" in ln for ln in lines)


# ── heartbeat + capabilities ─────────────────────────────────────────────────────────────────
def test_the_heartbeat_advertises_what_this_node_can_run(client, api):
    name = worker_mod.heartbeat(client)
    assert name == api.worker_name
    sent = api.heartbeats[-1]
    assert "crawl_all" in sent["capabilities"]


def test_capabilities_can_be_pinned_to_a_subset(monkeypatch):
    """How a node is dedicated to one task type, or used to drain one without touching the rest."""
    monkeypatch.setenv("CRAWLFAST_WORKER_TASK_TYPES", "crawl_single, crawl_pages")
    assert worker_mod._capabilities() == ["crawl_pages", "crawl_single"]


def test_an_empty_or_nonsense_pin_falls_back_to_everything(monkeypatch):
    """A typo in the env must not silently make the node claim nothing at all — which reads
    exactly like the wedged-claim-loop failure."""
    monkeypatch.setenv("CRAWLFAST_WORKER_TASK_TYPES", "not_a_type")
    assert worker_mod._capabilities() == worker_mod.supported_task_types()
    monkeypatch.setenv("CRAWLFAST_WORKER_TASK_TYPES", "   ")
    assert worker_mod._capabilities() == worker_mod.supported_task_types()


# ── the --once / cron mode ───────────────────────────────────────────────────────────────────
def test_once_mode_runs_a_single_cycle_and_exits_zero(api, site, tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWLFAST_WORKER_API_BASE_URL", api.url)
    monkeypatch.setenv("CRAWLFAST_WORKER_API_KEY", "test-key")
    monkeypatch.setenv("CRAWLFAST_WORKER_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("CRAWLFAST_WORKER_LOG_DIR", str(tmp_path / "logs"))
    api.queue(_crawl_task(site))

    assert worker_mod.main(["--once", "--config", str(tmp_path / "none.yaml")]) == 0
    assert api.results[-1]["status"] == "succeeded"


def test_health_mode_reports_and_exits_zero(api, tmp_path, monkeypatch):
    monkeypatch.setenv("CRAWLFAST_WORKER_API_BASE_URL", api.url)
    monkeypatch.setenv("CRAWLFAST_WORKER_API_KEY", "test-key")
    assert worker_mod.main(["--health", "--config", str(tmp_path / "none.yaml")]) == 0


def test_once_mode_returns_nonzero_when_the_api_is_unreachable(tmp_path, monkeypatch):
    """Cron needs a non-zero exit to notice; a silent 0 makes a dead node look healthy."""
    monkeypatch.setenv("CRAWLFAST_WORKER_API_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CRAWLFAST_WORKER_API_KEY", "k")
    monkeypatch.setenv("CRAWLFAST_WORKER_SPOOL", str(tmp_path / "spool"))
    assert worker_mod.main(["--once", "--config", str(tmp_path / "none.yaml")]) == 1
