"""Character encoding — the bug that corrupted pages without ever failing.

`requests` follows RFC 2616: a `text/*` response whose Content-Type carries no `charset`
parameter is decoded as **ISO-8859-1**. Most servers send exactly that and declare the real
charset in a `<meta charset="utf-8">` inside the document instead.

So the crawler read every such page as latin-1 and stored the mojibake — in S3, in the title, in
every meta description. Nothing raised, no page was "failed", the crawl reported success. On a
fleet pointed largely at Greek sites, that is most of the corpus, and it is invisible until
somebody reads the stored HTML.
"""

from __future__ import annotations

import pytest

from crawlfast_external_worker import executor
from crawlfast_external_worker.executor import _decode_html

GREEK = "Ενοικιάσεις Αυτοκινήτων — Ελλάδα"
PAGE = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{GREEK}</title><meta name="description" content="{GREEK}"></head>
<body><h1>{GREEK}</h1></body></html>"""


# ── the decoder in isolation ─────────────────────────────────────────────────────────────────
def test_the_header_charset_wins():
    raw = GREEK.encode("utf-8")
    assert _decode_html(raw, "text/html; charset=utf-8") == GREEK


def test_a_meta_declaration_is_honoured_when_the_header_is_silent():
    """THE regression. `text/html` with no charset used to mean latin-1."""
    assert GREEK in _decode_html(PAGE.encode("utf-8"), "text/html")


def test_utf8_is_tried_before_giving_up():
    """No header charset, no meta tag — a clean UTF-8 decode is itself the proof it is UTF-8."""
    raw = f"<html><body>{GREEK}</body></html>".encode("utf-8")
    assert GREEK in _decode_html(raw, "text/html")


def test_a_genuine_latin1_page_still_decodes():
    text = "Café Ürüñ"
    assert _decode_html(text.encode("cp1252"), "text/html; charset=windows-1252") == text


def test_an_unknown_or_lying_charset_never_raises():
    """A crawler must return SOMETHING for every page it fetched. Raising here would turn a
    mislabelled page into an error and lose it."""
    assert _decode_html("héllo".encode("utf-8"), "text/html; charset=not-a-real-codec")
    assert _decode_html(b"\xff\xfe\x00bad bytes", "text/html")


def test_an_empty_body_is_an_empty_string():
    assert _decode_html(b"", "text/html") == ""


# ── end to end, over HTTP, through the real fetch path ───────────────────────────────────────
def test_a_utf8_page_served_without_a_charset_header_is_not_mojibake(site, cfg):
    """The fixture server sends bare `text/html`, exactly like the sites this fleet crawls."""
    site.set("/greek.html", status=200, content_type="text/html", body=PAGE)
    result = executor.execute(
        {"id": "t1", "task_type": "crawl_single", "payload": {"url": f"{site.url}/greek.html"}},
        cfg,
    )
    assert result["title"] == GREEK, f"mojibake in the stored title: {result['title']!r}"
    assert result["meta"]["description"] == GREEK


def test_the_html_shipped_to_the_server_is_the_real_text(site, cfg):
    """It is the HTML, not the title, that is persisted to S3 and read by everything downstream."""
    site.set("/greek.html", status=200, content_type="text/html", body=PAGE)
    shipped = []
    executor.execute(
        {"id": "t1", "task_type": "crawl_single", "payload": {"url": f"{site.url}/greek.html"}},
        cfg, on_page=lambda page: shipped.append(page) or True,
    )
    assert GREEK in shipped[0]["html"]
    assert "Î•Î»Î»Î¬Î´Î±" not in shipped[0]["html"]


def test_links_are_still_found_on_a_non_ascii_page(site, cfg):
    """Decoding is upstream of link extraction: get it wrong and the BFS walks garbage."""
    body = PAGE.replace("</body>", '<a href="/about.html">Σχετικά</a></body>')
    site.set("/greek.html", status=200, content_type="text/html", body=body)
    result = executor.execute(
        {"id": "t1", "task_type": "crawl_all",
         "payload": {"url": f"{site.url}/greek.html", "max_pages": 5}},
        cfg,
    )
    assert any(p["url"].endswith("/about.html") for p in result["pages"])
