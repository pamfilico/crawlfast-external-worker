"""The self-heal watchdog.

`worker.main`'s loop swallows every exception on purpose — a node on someone's home wifi must not
die on a blip. The cost is that a *sustained* failure is invisible: the node keeps heartbeating
while claiming nothing, which is exactly the "online but running 0" zombie an operator used to have
to SSH in and restart by hand (NODE-OPS.md).

So no forward progress for WORKER_WATCHDOG_IDLE_SECONDS exits non-zero, and `restart:
unless-stopped` respawns a fresh worker with fresh connections. These tests hold that promise:
the loop must exit when wedged, and must NOT exit merely because the queue is empty.
"""

from __future__ import annotations

import pytest

from crawlfast_external_worker import worker as worker_mod


class _Exited(Exception):
    """Stands in for os._exit, which would take pytest down with it."""


@pytest.fixture
def no_hard_exit(monkeypatch):
    def fake_exit(code):
        raise _Exited(code)

    monkeypatch.setattr(worker_mod.os, "_exit", fake_exit)
    monkeypatch.setattr(worker_mod.time, "sleep", lambda s: None)


@pytest.fixture
def fast_clock(monkeypatch):
    """A clock that jumps a minute per read, so a 600s watchdog trips in a few iterations."""
    ticks = {"now": 1_000_000.0}

    def now():
        ticks["now"] += 60.0
        return ticks["now"]

    monkeypatch.setattr(worker_mod.time, "time", now)
    return ticks


def _env(monkeypatch, api, tmp_path, idle="600"):
    monkeypatch.setenv("CRAWLFAST_WORKER_API_BASE_URL", api.url)
    monkeypatch.setenv("CRAWLFAST_WORKER_API_KEY", "test-key")
    monkeypatch.setenv("CRAWLFAST_WORKER_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("CRAWLFAST_WORKER_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("WORKER_WATCHDOG_IDLE_SECONDS", idle)
    return ["--config", str(tmp_path / "none.yaml")]


def test_a_wedged_claim_loop_exits_for_restart(api, tmp_path, monkeypatch, no_hard_exit, fast_clock):
    """The claim endpoint keeps failing while heartbeats succeed — the classic wedge."""
    argv = _env(monkeypatch, api, tmp_path)
    api.fail_next["/api/v1/external-worker/tasks/claim"] = 10_000

    with pytest.raises(_Exited) as exc:
        worker_mod.main(argv)
    assert exc.value.args[0] == 1, "a wedged node must exit NON-zero so the restart policy fires"


def test_an_idle_but_healthy_node_also_restarts_rather_than_idling_forever(
    api, tmp_path, monkeypatch, no_hard_exit, fast_clock
):
    """No task processed for the whole window is treated as broken, not idle.

    Deliberate: the crawl queue is effectively always deep, so a node that claims nothing for ten
    minutes is far more likely to be wedged than genuinely out of work — and a needless restart
    costs seconds, while a missed wedge costs a day of that node's throughput.
    """
    argv = _env(monkeypatch, api, tmp_path)
    with pytest.raises(_Exited):
        worker_mod.main(argv)


def test_progress_resets_the_watchdog(api, site, tmp_path, monkeypatch, no_hard_exit):
    """A node that IS working must never be restarted underneath a running crawl."""
    argv = _env(monkeypatch, api, tmp_path, idle="600")
    for i in range(3):
        api.queue({"id": f"t{i}", "task_type": "crawl_single",
                   "payload": {"url": f"{site.url}/about.html"}})

    # Real clock, tiny window of work: the loop drains three tasks well inside 600s, so the
    # watchdog must not fire. Stop it by making the fourth claim raise KeyboardInterrupt.
    calls = {"n": 0}
    real_claim = worker_mod.CrawlfastWorkerClient.claim_task

    def claim(self):
        calls["n"] += 1
        if calls["n"] > 4:
            raise KeyboardInterrupt
        return real_claim(self)

    monkeypatch.setattr(worker_mod.CrawlfastWorkerClient, "claim_task", claim)
    assert worker_mod.main(argv) == 0, "a working node exited — the watchdog fired on progress"
    assert len(api.results) == 3
