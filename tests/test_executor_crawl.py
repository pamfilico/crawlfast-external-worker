"""The crawler, against real HTML served over real HTTP.

Every assertion here is a bug that has actually cost pages in production. The comments name which.
A crawl that "succeeds" while quietly dropping two thirds of a site is the failure mode this file
exists to make impossible — see CRAWL_HEALTH.md ("`completed` does not mean the content is there").
"""

from __future__ import annotations

import pytest

from crawlfast_external_worker import executor


@pytest.fixture
def collected():
    """Stand-in for the worker's `on_page`: records what would have been shipped to the server."""
    pages = []

    def on_page(page):
        pages.append(page)
        return True

    on_page.pages = pages
    return on_page


def _crawl(site, cfg, collected, *, max_pages=50, path="/index.html"):
    return executor.execute(
        {"id": "t1", "task_type": "crawl_all",
         "payload": {"url": f"{site.url}{path}", "max_pages": max_pages}},
        cfg, on_page=collected,
    )


# ── the whole site ───────────────────────────────────────────────────────────────────────────
def test_it_follows_links_and_stores_every_page(site, cfg, collected):
    result = _crawl(site, cfg, collected)
    assert result["pages_crawled"] >= 6, "the BFS did not leave the seed page"
    assert result["errors"] == 0, f"errors during a healthy crawl: {result['pages']}"
    assert result["pages_saved"] == result["pages_crawled"], "a crawled page was not persisted"

    got = {p["url"].replace(site.url, "") for p in collected.pages}
    assert {"/index.html", "/about.html", "/contact.html", "/docs/guide.html"} <= got

    # Every page ships its FULL html — the server, not the node, owns storage.
    assert all(p.get("html") for p in collected.pages), "a page was shipped with no html"
    assert any("FIXTURE::about" in p["html"] for p in collected.pages)


def test_the_entity_encoded_query_is_decoded_before_it_is_requested(site, cfg, collected):
    """`href="?a=1&amp;b=2"` is the URL `?a=1&b=2`. Requesting the literal `&amp;` 404s — this
    lost 41 of 50 pages on orfanakisbike.gr."""
    _crawl(site, cfg, collected)
    hits = [h for h in site.hits if h.startswith("/search.html")]
    assert hits, "the entity-encoded link was never followed"
    assert all("&amp;" not in h for h in hits), f"requested the raw entity: {hits}"
    assert any(h == "/search.html?a=1&b=2" for h in hits), hits


def test_a_duplicated_path_segment_is_collapsed(site, cfg, collected):
    """`/en/en/team.html` is a relative-link quirk; the page that exists is `/en/team.html`."""
    _crawl(site, cfg, collected)
    assert site.fetched("/en/team.html"), "the normalized URL was never fetched"
    assert not any(h.startswith("/en/en/") for h in site.hits), \
        f"crawled the duplicated segment as-is: {site.hits}"


def test_assets_are_never_fetched(site, cfg, collected):
    """Each wasted asset fetch is a page slot and a timeout. The check is on the PATH, because the
    URL usually carries a cache-busting query (`style.css?ver=3.7.6`) that hides the extension."""
    _crawl(site, cfg, collected)
    for asset in ("/assets/style.css", "/assets/app.js", "/assets/brochure.pdf",
                  "/assets/photo.jpg", "/min"):
        assert not any(h.startswith(asset) for h in site.hits), f"fetched the asset {asset}"


def test_it_never_leaves_the_host_or_follows_other_schemes(site, cfg, collected):
    """The loopback guard in conftest would turn an off-site link into a hard error; this asserts
    the intent directly so the reason is legible when it fires."""
    result = _crawl(site, cfg, collected)
    assert all(p["url"].startswith(site.url) for p in collected.pages)
    assert result["errors"] == 0


def test_the_same_page_is_not_crawled_twice(site, cfg, collected):
    """/about.html is linked absolutely, relatively, and with a fragment."""
    _crawl(site, cfg, collected)
    assert site.fetched("/about.html") == 1, \
        f"about.html fetched {site.fetched('/about.html')} times: {site.hits}"


def test_max_pages_is_honoured(site, cfg, collected):
    result = _crawl(site, cfg, collected, max_pages=2)
    assert result["pages_crawled"] == 2
    assert result["links_found"] > 2, "links_found counts what was DISCOVERED, not what was fetched"


# ── failure handling ─────────────────────────────────────────────────────────────────────────
def test_one_broken_page_does_not_abort_the_crawl(site, cfg, collected):
    """A crawl that stops at the first 500 loses the rest of a working site."""
    site.set("/contact.html", status=500, body="server error")
    result = _crawl(site, cfg, collected)
    assert result["pages_crawled"] >= 5
    statuses = {p.get("http_status") for p in result["pages"]}
    assert 500 in statuses, "the failure must be REPORTED, not swallowed"


def test_a_non_html_response_is_not_parsed_for_links(site, cfg, collected):
    """A PDF or binary that slipped through has no pages to follow; reading it downloads megabytes
    and extracts junk."""
    site.set("/docs/guide.html", status=200, content_type="application/pdf", body="%PDF-1.4 xx")
    result = _crawl(site, cfg, collected)
    guide = next(p for p in result["pages"] if p["url"].endswith("/docs/guide.html"))
    assert guide["content_length"] == 0, "a non-HTML body should not have been read"


def test_a_429_is_retried_once_after_retry_after(site, cfg, collected, monkeypatch):
    """Most rate limits are momentary. Retrying once turns a would-be failure into a save without
    hammering the site — the fleet's own hammering is what produced 403s in the first place."""
    slept = []
    monkeypatch.setattr(executor.time, "sleep", lambda s: slept.append(s))
    state = {"first": True}
    original = site.set

    site.set("/about.html", status=429, headers={"Retry-After": "2"}, body="slow down")
    _crawl(site, cfg, collected)
    assert site.fetched("/about.html") == 2, "a 429 must be retried exactly once"
    assert 2.0 in slept or any(s > 0 for s in slept), f"did not wait: {slept}"
    del original, state


def test_the_retry_can_be_switched_off(site, cfg, collected, monkeypatch):
    monkeypatch.setenv("CRAWLFAST_WORKER_NO_RETRY", "1")
    site.set("/about.html", status=429, headers={"Retry-After": "2"}, body="slow down")
    _crawl(site, cfg, collected)
    assert site.fetched("/about.html") == 1


# ── the other task types ─────────────────────────────────────────────────────────────────────
def test_crawl_pages_retries_only_the_urls_it_was_given(site, cfg, collected):
    """This is what `rescrape-failed` sends: repair the broken pages without re-fetching the good
    ones. A BFS here would re-crawl the whole site and undo the point of the repair."""
    result = executor.execute(
        {"id": "t2", "task_type": "crawl_pages",
         "payload": {"pages": [f"{site.url}/about.html", f"{site.url}/contact.html"]}},
        cfg, on_page=collected,
    )
    assert result["pages_crawled"] == 2
    assert result["pages_saved"] == 2
    assert not site.fetched("/docs/guide.html"), "it walked links instead of the given list"


def test_lite_fetch_returns_title_and_meta(site, cfg, collected):
    result = executor.execute(
        {"id": "t3", "task_type": "crawl_single", "payload": {"url": f"{site.url}/index.html"}},
        cfg, on_page=collected,
    )
    assert result["title"] == "Fixture Site — Home"
    assert result["meta"]["description"].startswith("The home page")
    assert result["pages_saved"] == 1


def test_an_unknown_task_type_raises(cfg):
    """The worker turns this into a `failed` result; silently succeeding would mark work done that
    never ran."""
    with pytest.raises(ValueError, match="no handler"):
        executor.execute({"id": "t4", "task_type": "not_a_task", "payload": {}}, cfg)


def test_supported_task_types_are_the_ones_the_server_queues(cfg):
    assert set(executor.supported_task_types()) == {
        "crawl", "crawl_all", "crawl_pages", "crawl_sitemap",
        "crawl_single", "crawl_single:meta", "extract_meta",
    }


def test_a_task_with_no_url_raises_rather_than_reporting_success(cfg):
    with pytest.raises(ValueError, match="no 'url'"):
        executor.execute({"id": "t5", "task_type": "crawl_all", "payload": {}}, cfg)
