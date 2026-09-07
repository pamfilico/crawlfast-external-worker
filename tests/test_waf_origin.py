"""The crawler against an origin that judges the request instead of the URL.

`tests/test_headers.py` asserts what we PUT on the wire. This file asserts what that buys: an
origin modelled on the one that refused us can be crawled end to end. The two are different
questions, and only this one would have caught enterprise.nl — where every header assertion you
could write against the old code would have passed, because the old header set was internally
consistent and simply not enough.

The failure being pinned: 47 pages recorded `failed`, every one `http 403`, one page saved out of
48 discovered, and a lead marked `completed` on the strength of that one page.
"""

from __future__ import annotations

import pytest

from crawlfast_external_worker import executor

from conftest import WAF_MIN_CHROME


def _crawl(site, cfg, on_page=None, *, max_pages=50, path="/index.html"):
    return executor.execute(
        {"id": "t", "task_type": "crawl_all",
         "payload": {"url": f"{site.url}{path}", "max_pages": max_pages}},
        cfg, on_page=on_page,
    )


# ── the fixture must be able to say no ───────────────────────────────────────────────────────
def test_the_fixture_waf_really_does_block(waf, cfg, monkeypatch):
    """Guard on the guard.

    Every other test in this file passes trivially against an origin that never refuses anything,
    and a fixture WAF that silently stopped enforcing would look exactly like a crawler that got
    better. So: put the OLD header set back and require the fixture to reject it, with the same
    `http 403` the real one produced.
    """
    monkeypatch.setattr(executor, "_UA", {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    result = _crawl(waf, cfg)
    statuses = {p.get("http_status") for p in result["pages"]}
    assert statuses == {403}, f"the fixture served something to the old header set: {statuses}"
    assert result["pages_saved"] == 0
    assert waf.denied, "the WAF recorded no denials"


def test_the_old_shape_produces_exactly_the_production_signature(waf, cfg, monkeypatch):
    """Not just "it failed" — the SHAPE of the failure, because that shape is what fooled us.

    A 403 arrives as a perfectly well-formed HTTP response carrying real HTML. Everything that
    only asked "did a response come back" saw success. The crawl then reported one saved page and
    a completed website, which is how a site we were fully locked out of came to look finished.
    """
    monkeypatch.setattr(executor, "_UA", {**executor._UA, "User-Agent": "Mozilla/5.0 Chrome/124.0.0.0"})
    result = _crawl(waf, cfg, max_pages=10)
    assert result["errors"] == 0, "a 403 is not an exception — it is a successful HTTP exchange"
    assert result["pages_crawled"] >= 1
    assert all((p.get("content_length") or 0) < 1000 for p in result["pages"]), (
        "the deny page is short but non-empty; a size floor is the only thing that separates it "
        "from a real page"
    )
    assert result["links_found"] == 1, "a deny page has no links, so the BFS cannot leave the seed"


# ── what the fix buys ────────────────────────────────────────────────────────────────────────
def test_the_shipped_crawler_gets_through(waf, cfg):
    """The regression. With the header set as shipped, the same origin serves the whole site."""
    pages = []
    result = _crawl(waf, cfg, on_page=lambda p: (pages.append(p), True)[1])
    assert not waf.denied, f"refused: {waf.denied}"
    assert result["pages_crawled"] > 1, "the BFS never left the seed page"
    assert result["errors"] == 0
    assert {p.get("http_status") for p in result["pages"]} == {200}
    assert result["pages_saved"] == result["pages_crawled"], "a fetched page was not persisted"


def test_a_repair_pass_also_gets_through(waf, cfg):
    """`crawl_pages` re-fetches URLs that already failed — often failed BECAUSE of the request
    shape. A repair that goes out weaker than the original fetch can only ever fail again."""
    result = executor.execute(
        {"id": "t", "task_type": "crawl_pages",
         "payload": {"pages": [f"{waf.url}/index.html", f"{waf.url}/about.html"]}},
        cfg,
    )
    assert not waf.denied
    assert {p.get("http_status") for p in result["pages"]} == {200}


def test_a_single_page_task_also_gets_through(waf, cfg):
    result = executor.execute(
        {"id": "t", "task_type": "crawl_single", "payload": {"url": f"{waf.url}/index.html"}}, cfg,
    )
    assert not waf.denied
    assert result["http_status"] == 200


# ── the version cliff, which is the part that arrives on its own ─────────────────────────────
@pytest.mark.parametrize("major,allowed", [(WAF_MIN_CHROME - 1, False), (WAF_MIN_CHROME, True)])
def test_the_chrome_version_alone_decides_the_verdict(waf, cfg, monkeypatch, major, allowed):
    """Everything else held identical — only the number in the User-Agent moves.

    This is the whole lesson of enterprise.nl in one parametrize: the crawler did not change, the
    site did not change, and a constant that was correct when written became a total block. Nothing
    in the codebase could see it, because nothing in the codebase was wrong.
    """
    monkeypatch.setattr(executor, "_UA", {
        **executor._UA,
        "User-Agent": (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                       f"Chrome/{major}.0.0.0 Safari/537.36"),
    })
    result = executor.execute(
        {"id": "t", "task_type": "crawl_single", "payload": {"url": f"{waf.url}/index.html"}}, cfg,
    )
    assert (result["http_status"] == 200) is allowed, (
        f"Chrome/{major} was {'refused' if allowed else 'served'} — the fixture's floor moved"
    )


def test_the_shipped_version_is_comfortably_above_the_floor(cfg):
    """The pin should not be sitting one release above the cliff. If this fails the pin is stale
    even though `test_the_pinned_chrome_version_has_not_gone_stale` may not have fired yet."""
    assert executor._CHROME_MAJOR >= WAF_MIN_CHROME + 5, (
        f"Chrome/{executor._CHROME_MAJOR} is close to the fixture's floor of {WAF_MIN_CHROME}; "
        "the real thresholds move upward, never down."
    )
