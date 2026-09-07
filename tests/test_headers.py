"""What we put on the wire — the request SHAPE, which is load-bearing.

enterprise.nl is why this file exists. 47 of its pages were recorded `failed`, every one `http 403`,
and nothing in the crawler was broken: Akamai was refusing the shape of the request. Reproduced
against the live site one request a minute, so rate limiting could not explain it:

    worker's three headers, Chrome/124 ......... 403
    full browser headers,   Chrome/139 ......... 200   (481 KB)
    full browser headers MINUS Sec-Fetch-* ..... 403
    full browser headers,   Chrome/124 ......... 403   <- only the version differs
    full browser headers,   Chrome/139 ......... 200

Two independent things move the verdict: the fetch-metadata headers, and the Chrome version. It
behaves like a score rather than a rulebook — any one header could go missing and still pass, three
at once could not — so the aim is to stop looking unusual, not to satisfy a checklist. The version
is the dangerous half: it was correct when written and rotted into a block on its own.

It is NOT pacing. 20 pages of the same origin back to back with zero delay all returned 200 once the
headers were right, so no amount of politeness would have recovered those 47 pages.
"""

from __future__ import annotations

import datetime
import re

import pytest

from crawlfast_external_worker import executor


# ── the pin that rots ────────────────────────────────────────────────────────────────────────
def test_the_pinned_chrome_version_has_not_gone_stale():
    """A hardcoded browser version is a bug with a delivery date.

    `Chrome/124` shipped, worked, and then — with no commit, no deploy, no change of any kind —
    started being answered 403 by Akamai-fronted origins, because no real browser reports it any
    more. The only defence against a constant that decays is to make its age fail CI.

    If this is failing: open a real Chrome, check `chrome://version`, set `_CHROME_MAJOR` to that
    major and `_UA_PINNED_ON` to today. That is the whole fix.
    """
    age = (datetime.date.today() - executor._UA_PINNED_ON).days
    assert age <= executor._UA_MAX_AGE_DAYS, (
        f"the User-Agent has been pinned to Chrome/{executor._CHROME_MAJOR} for {age} days "
        f"(limit {executor._UA_MAX_AGE_DAYS}). It is drifting towards the version cliff that cost "
        "47 pages of enterprise.nl. Bump _CHROME_MAJOR to a current Chrome and move _UA_PINNED_ON."
    )


def test_the_user_agent_and_the_client_hints_claim_the_same_version():
    """A browser cannot disagree with itself, so a mismatch here is a bot signal we would be
    handing out for free. Both strings are built from `_CHROME_MAJOR`; this proves they stay that
    way if someone edits one of them by hand."""
    major = str(executor._CHROME_MAJOR)
    ua_major = re.search(r"Chrome/(\d+)\.", executor._UA["User-Agent"]).group(1)
    assert ua_major == major
    assert f'"Google Chrome";v="{major}"' in executor._UA["sec-ch-ua"]


# ── what every fetch must carry ──────────────────────────────────────────────────────────────
REQUIRED = ("sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "user-agent", "accept",
            "accept-language")


def test_a_single_page_fetch_sends_the_fetch_metadata_headers(site, cfg):
    """Removing `Sec-Fetch-*` from an otherwise perfect browser header set turned 200 into 403 on
    the live origin. Every real navigation sends them; their absence is itself the signal."""
    executor.execute(
        {"id": "t", "task_type": "crawl_single", "payload": {"url": f"{site.url}/index.html"}},
        cfg,
    )
    headers = site.headers_for("/index.html")
    missing = [h for h in REQUIRED if h not in headers]
    assert not missing, f"fetch went out without {missing}"
    assert headers["sec-fetch-mode"] == "navigate"


def test_the_seed_is_a_typed_url_and_an_interior_page_is_a_click(site, cfg):
    """`Sec-Fetch-Site: none` means "the user typed this". True of the seed; a lie for the other
    forty-nine pages of a BFS, and fifty consecutive typed navigations to one host is a pattern no
    human produces. Interior pages carry the page that linked to them instead."""
    result = executor.execute(
        {"id": "t", "task_type": "crawl_all",
         "payload": {"url": f"{site.url}/index.html", "max_pages": 50}},
        cfg,
    )
    assert result["pages_crawled"] > 1, "need an interior page to make this assertion"

    seed = site.headers_for("/index.html")
    assert seed["sec-fetch-site"] == "none"
    assert "referer" not in seed, "the seed URL was not reached from anywhere"

    interior_path, interior = next(
        (path, {k.lower(): v for k, v in h.items()})
        for path, h in site.requests if path != "/index.html"
    )
    assert interior["sec-fetch-site"] == "same-origin", interior_path
    assert interior.get("referer", "").endswith("/index.html"), (
        f"{interior_path} did not say which page linked to it: {interior.get('referer')!r}"
    )


def test_a_repair_fetch_still_looks_like_a_browser(site, cfg):
    """`crawl_pages` re-fetches URLs that already failed once — often failed *because* of the
    request shape. It would be perverse for the retry to go out with a weaker one."""
    executor.execute(
        {"id": "t", "task_type": "crawl_pages",
         "payload": {"pages": [f"{site.url}/about.html"]}},
        cfg,
    )
    headers = site.headers_for("/about.html")
    assert not [h for h in REQUIRED if h not in headers]


@pytest.mark.parametrize("header", ["accept-encoding"])
def test_we_do_not_advertise_an_encoding_we_cannot_decode(site, cfg, header):
    """`Accept-Encoding` is left to requests/urllib3 on purpose: it advertises exactly what it can
    decompress. Setting it by hand to a browser's `gzip, deflate, br, zstd` buys a body of binary
    noise on any origin that takes us up on it, and it was proven irrelevant to the block."""
    assert header not in {k.lower() for k in executor._UA}
