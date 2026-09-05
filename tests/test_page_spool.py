"""The page spool — never silently lose a crawled page.

The outcome of a page POST is the SERVER'S verdict, not the HTTP status. The old code returned
True on any 200, so a page the server refused was counted as saved: that is the `saved=1` while
seven pages were crawled bug. And when the POST itself fails (server restarting -> timeout), the
page goes to disk rather than being dropped, because the task would otherwise report success with
the content missing.
"""

from __future__ import annotations

import json
import os

import pytest

from crawlfast_external_worker.page_spool import PageSpool


@pytest.fixture
def spool(tmp_path):
    return PageSpool(str(tmp_path / "spool"))


PAGE = {"url": "http://x/p1", "html": "<html>p1</html>"}


def test_a_saved_page_is_reported_saved(spool, client, api):
    outcome, info = spool.submit_with_retry(client, "t1", PAGE)
    assert outcome == "saved"
    assert info["saved"] is True
    assert spool.pending() == [], "a saved page must not also be spooled"


def test_a_server_refusal_is_rejected_not_saved_and_not_spooled(spool, client, api):
    """`rejected` means the server got the page and won't keep it — a 403 block page, non-HTML,
    empty. Retrying cannot change the content, so spooling it would loop forever."""
    api.reject_pages = True
    outcome, info = spool.submit_with_retry(client, "t1", PAGE)
    assert outcome == "rejected"
    assert info["saved"] is False
    assert spool.pending() == [], "a rejected page must never be spooled — the retry can't help"


def test_a_failing_post_is_retried_then_spooled(spool, client, api):
    """The POST itself failing is transient (server slow/restarting). The page goes to disk so the
    next flush can land it, instead of the task 'succeeding' with the content missing."""
    api.fail_next["/api/v1/external-worker/tasks/t1/page"] = 99
    outcome, _info = spool.submit_with_retry(client, "t1", PAGE, retries=2)
    assert outcome == "spooled"
    assert len(spool.pending()) == 1
    written = json.loads(open(spool.pending()[0]).read())
    assert written["task_id"] == "t1"
    assert written["page"]["html"] == PAGE["html"], "the html must survive — it is the whole point"


def test_a_blip_is_retried_and_the_page_still_lands(spool, client, api):
    api.fail_next["/api/v1/external-worker/tasks/t1/page"] = 1
    outcome, _ = spool.submit_with_retry(client, "t1", PAGE, retries=3)
    assert outcome == "saved"
    assert spool.pending() == []


def test_flush_lands_spooled_pages_and_deletes_them(spool, client, api):
    api.fail_next["/api/v1/external-worker/tasks/t1/page"] = 99
    spool.submit_with_retry(client, "t1", PAGE, retries=1)
    api.fail_next.clear()

    recovered, dropped = spool.flush(client)
    assert (recovered, dropped) == (1, 0)
    assert spool.pending() == []
    assert api.pages[-1]["html"] == PAGE["html"]


def test_flush_drops_a_page_whose_task_is_gone(spool, client, api, monkeypatch):
    """A task reclaimed by `reclaim_stale` and given to another node can never be landed by this
    one. Keeping it would flush it forever — the 1.27 GB of looping uploads in CRAWL_TASK_LEASE.md.
    """
    api.fail_next["/api/v1/external-worker/tasks/t1/page"] = 99
    spool.submit_with_retry(client, "t1", PAGE, retries=1)
    api.fail_next.clear()

    def gone(task_id, page):
        raise RuntimeError("404: task not assigned to this worker")

    monkeypatch.setattr(client, "submit_page", gone)
    recovered, dropped = spool.flush(client)
    assert (recovered, dropped) == (0, 1)
    assert spool.pending() == [], "a permanently-rejected page must be dropped, not re-flushed"


def test_flush_keeps_a_transient_failure_for_next_time(spool, client, api, monkeypatch):
    api.fail_next["/api/v1/external-worker/tasks/t1/page"] = 99
    spool.submit_with_retry(client, "t1", PAGE, retries=1)

    def timeout(task_id, page):
        raise RuntimeError("request to /page failed: read timeout")

    monkeypatch.setattr(client, "submit_page", timeout)
    recovered, dropped = spool.flush(client)
    assert (recovered, dropped) == (0, 0)
    assert len(spool.pending()) == 1, "a timeout is not a refusal — the page must be kept"


def test_a_corrupt_spool_file_is_discarded_not_fatal(spool, client, api, tmp_path):
    path = os.path.join(spool.path, "broken.json")
    with open(path, "w") as fh:
        fh.write("{not json")
    recovered, dropped = spool.flush(client)
    assert spool.pending() == []
    assert (recovered, dropped) == (0, 0)


def test_flush_is_bounded(spool, client, api):
    """A huge spool must not block the crawl for minutes on one flush."""
    api.fail_next["/api/v1/external-worker/tasks/t1/page"] = 999
    for i in range(5):
        spool.submit_with_retry(client, "t1", {**PAGE, "url": f"http://x/p{i}"}, retries=1)
    api.fail_next.clear()
    recovered, _ = spool.flush(client, max_files=2)
    assert recovered == 2
    assert len(spool.pending()) == 3


def test_the_spool_survives_a_restart(client, api, tmp_path):
    """Plain JSON files on disk, so a worker that is killed mid-crawl still lands its pages."""
    path = str(tmp_path / "spool")
    api.fail_next["/api/v1/external-worker/tasks/t1/page"] = 99
    PageSpool(path).submit_with_retry(client, "t1", PAGE, retries=1)
    api.fail_next.clear()

    reborn = PageSpool(path)
    assert len(reborn.pending()) == 1
    assert reborn.flush(client) == (1, 0)
