"""Task executor — turns a claimed task into a result dict.

Pure REST worker: it knows NOTHING about the backend's environment. Every parameter it needs
(the URL, how many pages, task type) arrives in the task payload over HTTP; the only things it
fetches are public web pages. Results (and incremental progress) go back over the REST API — the
server owns all storage/DB. No Spaces, no database, no app env.

Handlers:
  crawl_single / crawl_single:meta / extract_meta  → fetch one page (title + meta)
  crawl_all / crawl / crawl_sitemap                → BFS the whole site (all same-host pages)

Both parse HTML with no browser (GET + regex). To reach full parity with the internal Playwright
worker later, register a heavier handler here — same signature.

Content persistence: the node owns NO storage. Every fetched page's full HTML is handed to the
``on_page`` callback (wired by the worker to POST it to the server, which saves raw.html to S3 and
creates the Page row — exactly like the native scraper). The returned result stays lightweight
(per-page metadata only); the HTML never rides back inside it.
"""

from __future__ import annotations

import re
import os
import socket
import time
import datetime as _dt
from urllib.parse import urljoin, urlparse

import requests

from html import unescape as _html_unescape  # entity-decode hrefs (&amp;→&); aliased so the
# `html` string param of _same_host_links can't shadow the module

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_HREF_RE = re.compile(r'href=["\']([^"\'#]+)["\']', re.IGNORECASE)
# Browser-shaped request headers: many sites' WAFs 403 a bot-identifying UA (or python-requests)
# but serve 200 to a browser (verified on boatrentalrethymno.gr — bot UA=403, browser UA=200).
# Crawling public pages with a normal browser shape is standard; it recovers gated sites that
# otherwise "fail" with 0 pages.
#
# A UA STRING ALONE IS NOT ENOUGH, and this was expensive to learn. enterprise.nl (Akamai) recorded
# 47 failed pages, every one `http 403`. Reproduced from a laptop against the live site, one request
# a minute so nothing could be blamed on rate limiting:
#
#     worker's three headers, Chrome/124 ............ 403   (371 bytes, "Access Denied")
#     full browser headers,   Chrome/139 ............ 200   (481 KB)
#     full browser headers MINUS Sec-Fetch-* ........ 403
#     full browser headers,   Chrome/124 ............ 403   <- only the version differs
#     full browser headers,   Chrome/139 ............ 200
#
# Same IP, same TLS stack, seconds apart. Two independent things move the verdict:
#
#   1. the fetch-metadata headers (`Sec-Fetch-*`) — every real navigation sends them, so their
#      absence is by itself a bot signal;
#   2. the Chrome major version — it must be one a real browser could still be reporting.
#
# It behaves like a SCORE, not a rulebook. Dropping any single header from the working set still
# returned 200; dropping three at once did not. So the goal is not to satisfy a checklist but to
# stop looking unusual, which is why the set below is a whole browser's worth rather than the
# minimum that happened to pass on the day.
#
# What it is NOT is pacing. The obvious theory — a BFS hammering fifty pages with no gap — is
# wrong here: 20 pages of this same origin, back to back with zero delay, all returned 200 once
# the headers were right. Page delay is still worth having for origins that genuinely rate-limit,
# but it would not have recovered a single one of these 47 pages.
#
# (2) is the trap, because it is not a bug that was written — it is a bug that ARRIVED. `Chrome/124`
# was current when it was pinned and was answered 200. It aged into a 403 while the file sat
# untouched. Nothing about the crawler changed; the world moved and the constant did not.
#
# Hence `_UA_PINNED_ON` and the test that fails when it goes stale: the only defence against a
# constant that rots is to make its age visible to CI. Bump `_CHROME_MAJOR` to whatever a current
# desktop Chrome reports and move the date with it.
_CHROME_MAJOR = 152
#: Day `_CHROME_MAJOR` was last checked against a real browser. See
#: `tests/test_headers.py::test_the_pinned_chrome_version_has_not_gone_stale`.
_UA_PINNED_ON = _dt.date(2026, 9, 8)
#: How long a pin may go unreviewed before CI fails. Chrome ships a major roughly every four weeks,
#: so six months is ~6-7 versions behind — still plausible, well before the cliff that caught us.
_UA_MAX_AGE_DAYS = 180

_UA = {
    "User-Agent": (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   f"Chrome/{_CHROME_MAJOR}.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
               "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": (f'"Chromium";v="{_CHROME_MAJOR}", "Not=A?Brand";v="24", '
                  f'"Google Chrome";v="{_CHROME_MAJOR}"'),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    # Overridden per request by _headers(): `none` is what a browser sends for a URL typed into the
    # address bar, which is the honest description of a seed URL and the only thing we can claim
    # for one. See _headers for why an interior page must say something else.
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
# Deliberately NOT set: `Accept-Encoding`. requests/urllib3 negotiates what it can actually decode,
# and advertising `br`/`zstd` we cannot decompress buys a body of binary noise. Proven irrelevant
# to the block above (removing it from the working set still returned 200).


def _headers(referer: str | None = None) -> dict:
    """Request headers for one fetch, describing how we arrived at this URL.

    A browser sends `Sec-Fetch-Site: none` only for a URL the user typed. Every page reached by
    clicking a link carries `same-origin` and a `Referer`. A BFS that claims fifty consecutive
    typed navigations to one host is describing something no human does — so interior pages say
    what actually happened: they were reached from the page that linked to them.
    """
    if not referer:
        return _UA
    return {**_UA, "Referer": referer, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1"}
# Non-page extensions to skip. Superset of the native scraper's image/pdf exclusion
# (.jpg/.jpeg/.png/.gif/.svg/.webp/.ico/.bmp/.tiff/.avif/.pdf) — matched for parity — PLUS the
# asset types a non-browser GET crawler must skip itself (css/js/fonts/media/data) that the native
# Playwright crawler never treats as page links.
_SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".tiff", ".avif",
             ".zip", ".mp4", ".css", ".js", ".ico", ".xml", ".json", ".woff", ".woff2", ".ttf")
# Path prefixes that are never real pages (infra/asset routes) — skip to avoid junk 404s.
_SKIP_PREFIXES = ("/cdn-cgi/", "/wp-json/", "/xmlrpc.php", "/feed")
# Asset-combiner / cache routes where the extension lives in the QUERY, e.g.
# `/css_combine?css_cache=abc.css`, `/min/?f=a.js`. The path has no extension so the path check
# misses them — match these substrings anywhere in the lowercased URL.
_SKIP_ASSET_HINTS = ("css_combine", "js_combine", "css_cache", "js_cache", ".css?", ".js?", "ai_skin=")


def _normalize_url(url: str) -> str:
    """Canonicalize a discovered URL so the BFS doesn't invent 404s and doesn't double-crawl.

    Collapses immediately-repeated path segments (``/en/en/company.php`` → ``/en/company.php``) —
    a common relative-link quirk that otherwise 404s — strips the fragment and any trailing slash,
    and preserves the query. Idempotent."""
    p = urlparse(url)
    out = []
    for seg in p.path.split("/"):
        if seg and out and out[-1] == seg:
            continue  # drop the duplicate (/en/en/ -> /en/)
        out.append(seg)
    path = "/".join(out).rstrip("/")
    return f"{p.scheme}://{p.netloc}{path}" + (f"?{p.query}" if p.query else "")

#: Pause between page fetches on ONE site, in seconds.
#:
#: The BFS walks a whole host back-to-back with no gap at all, which is what provokes the rate
#: limiting we then record as a permanent "blocked". billysrentacar.gr answered 403 to every client
#: we had after sustained probing and served 200 to all of them once left alone — the block was
#: ours to cause.
#:
#: Default 0 keeps every deployed node byte-identical until it is set. The SERVER can raise it per
#: task via ``payload.page_delay_seconds``, which is how a recrawl asks to be gentler than a first
#: pass: by the time we are re-fetching a page, the site has already pushed back once.
_PAGE_DELAY_ENV = "CRAWLFAST_WORKER_PAGE_DELAY_SECONDS"
_MAX_PAGE_DELAY = 30.0


def _page_delay(task: dict, cfg) -> float:
    """Seconds to wait between pages: the task's own value wins, else the node default, else none."""
    payload = task.get("payload") or {}
    raw = payload.get("page_delay_seconds")
    if raw is None:
        raw = getattr(cfg, "page_delay_seconds", None)
    if raw is None:
        raw = os.getenv(_PAGE_DELAY_ENV, "0")
    try:
        return max(0.0, min(float(raw), _MAX_PAGE_DELAY))
    except (TypeError, ValueError):
        return 0.0


_HANDLERS: dict[str, callable] = {}


def handler(*task_types: str):
    def deco(fn):
        for t in task_types:
            _HANDLERS[t] = fn
        return fn

    return deco


def _extract_meta(html: str, limit: int = 12) -> dict:
    out = {}
    for name, content in _META_RE.findall(html or ""):
        key = name.lower()
        if key in ("description", "keywords", "og:title", "og:description", "og:site_name"):
            out[key] = content[:500]
        if len(out) >= limit:
            break
    return out


def _title(html: str):
    m = _TITLE_RE.search(html or "")
    return m.group(1).strip()[:300] if m else None


def _retry_after_seconds(resp, cap=8.0):
    """Parse a Retry-After header (seconds form) and cap it, so a 429/503 gets ONE polite wait+retry
    instead of becoming a permanent failure. Ignores HTTP-date form (rare here) and absurd values."""
    ra = (resp.headers.get("Retry-After") or "").strip()
    try:
        return max(0.0, min(float(ra), cap)) if ra else min(2.0, cap)
    except ValueError:
        return min(2.0, cap)


#: Pooled session for TARGET fetches, created on first use when the node opts in. A BFS walks 50
#: pages of ONE host in sequence, and each page currently pays a fresh TCP+TLS handshake to a host
#: it is already talking to — measured at ~220ms per page against a live target.
_SESSION = None


def _fetcher(cfg):
    """Return the callable used to GET a page: a pooled Session when the node opted in, otherwise
    the module-level ``requests`` (byte-for-byte the original behaviour)."""
    global _SESSION
    if not getattr(cfg, "http_session", False):
        return requests
    if _SESSION is None:
        _SESSION = requests.Session()
    return _SESSION


class _Closed:
    """Stand-in for a response that never arrived, so the fallback loop can close() uniformly."""

    def close(self):
        pass


def _url_variants(url: str) -> list:
    """The other spellings of the same root domain, in the order worth trying.

    https before http (every canonical we see is https), then the opposite www-ness, then both.
    Never invents a different host — only the scheme and the `www.` prefix change, so this can
    reach a site that moved, never a site that is not the lead's.
    """
    p = urlparse(url)
    host = p.netloc
    other_host = host[4:] if host.startswith("www.") else f"www.{host}"
    rest = url.split(p.netloc, 1)[1] if p.netloc in url else ""
    out, seen = [], {url}
    for scheme in ("https", "http"):
        for h in (host, other_host):
            candidate = f"{scheme}://{h}{rest}"
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def _fetch(url: str, cfg, try_variants: bool = False, referer: str | None = None) -> dict:
    started = time.time()
    headers = _headers(referer)
    http = _fetcher(cfg)
    # 12s default (was 30) so a throttling/slow site can't hold a worker hostage for the full crawl.
    timeout = getattr(cfg, "request_timeout_seconds", 12.0)
    resp = http.get(url, timeout=timeout, headers=headers, allow_redirects=True, stream=True)
    # ROOT-DOMAIN FALLBACK. A lead carries whatever URL it was discovered with, and that is often
    # not the one the site actually serves: billysrentacar.gr answered 403 on
    # `http://www.billysrentacar.gr/` while `http://billysrentacar.gr/` returned 200 in the same
    # crawl, and its own canonical is https. Writing the lead off as dead because of a stale `www.`
    # or a stale `http://` throws away a perfectly good site.
    #
    # So on a 4xx/5xx, walk the other three spellings of the same root domain and keep the first
    # that answers. Only ever runs AFTER a failure, so a healthy site costs exactly one request.
    if resp.status_code >= 400 and try_variants and os.getenv("CRAWLFAST_WORKER_NO_URL_VARIANTS") != "1":
        first = resp
        for candidate in _url_variants(url):
            first.close()
            try:
                alt = http.get(candidate, timeout=timeout, headers=headers, allow_redirects=True, stream=True)
            except requests.RequestException:
                first = _Closed()
                continue
            if alt.status_code < 400:
                url, resp = candidate, alt
                break
            first = alt
        else:
            # Nothing better than the original; re-fetch it so the caller reports the real failure.
            first.close()
            resp = http.get(url, timeout=timeout, headers=headers, allow_redirects=True, stream=True)
    # Politeness / anti-rate-limit: on a 429/503, wait the (capped) Retry-After and retry ONCE. Most
    # rate-limits are momentary — this turns a would-be failure into a save without hammering. Off
    # via CRAWLFAST_WORKER_NO_RETRY=1.
    if resp.status_code in (429, 503) and os.getenv("CRAWLFAST_WORKER_NO_RETRY") != "1":
        wait = _retry_after_seconds(resp)
        resp.close()
        time.sleep(wait)
        resp = http.get(url, timeout=timeout, headers=headers, allow_redirects=True, stream=True)
    # Only read/parse HTML. A non-HTML response (asset, PDF, binary) that slipped through has no
    # pages to follow — skip the body so we don't download megabytes or extract junk links.
    ctype = (resp.headers.get("Content-Type") or "").lower()
    is_html = "html" in ctype or ctype == ""
    html = ""
    if is_html:
        # Cap the body (~3MB) so a pathological page can't blow up memory/time. Read BYTES and
        # decode them ourselves — see _decode_html for why requests' own decoding cannot be used.
        chunks, size = [], 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            size += len(chunk)
            if size > 3_000_000:
                break
        html = _decode_html(b"".join(chunks), resp.headers.get("Content-Type"))
    resp.close()
    return {
        "url": url,
        "final_url": resp.url,
        "http_status": resp.status_code,
        "elapsed_ms": int((time.time() - started) * 1000),
        "content_length": len(html),
        "title": _title(html),
        "meta": _extract_meta(html),
        "_html": html,
    }


_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I
)


def _header_charset(content_type: str | None) -> str | None:
    for part in (content_type or "").split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip("\"'") or None
    return None


def _decode_html(raw: bytes, content_type: str | None) -> str:
    """Bytes -> text, resolving the charset the way a browser does.

    NOT `resp.text` and NOT `iter_content(decode_unicode=True)`. Both use `resp.encoding`, and
    requests follows RFC 2616 by defaulting a `text/*` response with no charset parameter to
    **ISO-8859-1**. Most sites send exactly that header and declare their real charset in a
    `<meta charset="utf-8">` instead — so every non-ASCII character on such a page came back
    mojibake ("Ελλάδα" -> "Î•Î»Î»Î¬Î´Î±") and was stored that way, in S3 and in every title and
    meta description read from it. On a crawler pointed mostly at Greek sites that is most of the
    corpus.

    Order of authority, highest first:
      1. the charset in the Content-Type header — the server said so explicitly;
      2. a `<meta charset>` / `<meta http-equiv>` declaration in the document;
      3. UTF-8, accepted only if the whole body decodes cleanly (a strict decode IS the test);
      4. cp1252, which maps every byte and so can never raise.
    """
    if not raw:
        return ""

    declared = _header_charset(content_type)
    if not declared:
        match = _META_CHARSET_RE.search(raw[:4096])
        if match:
            declared = match.group(1).decode("ascii", "ignore")

    for candidate in (declared, "utf-8"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    # Last resort: a single-byte codec with no undefined slots, so this always returns something.
    return raw.decode("cp1252", "replace")


def _same_host_links(html: str, base_url: str, host: str) -> list[str]:
    links = []
    for raw_href in _HREF_RE.findall(html or ""):
        try:
            # Decode HTML entities in the href BEFORE using it: `?a=1&amp;b=2` in the markup is the
            # URL `?a=1&b=2` — requesting the literal `&amp;` 404s (verified on orfanakisbike.gr,
            # 41/50 pages lost). Also handles &#38; / &#x26; etc.
            href = _html_unescape(raw_href)
            if href.strip().lower().startswith(("mailto:", "tel:", "javascript:", "data:")):
                continue
            absu = _normalize_url(urljoin(base_url, href).split("#")[0])
            p = urlparse(absu)
            if p.scheme not in ("http", "https") or p.netloc != host:
                continue
            # Skip static assets by PATH extension — the full URL often carries a cache-busting
            # query (`style.css?ver=3.7.6`), so checking the whole URL misses them and the crawler
            # wastes a full fetch (+ timeout) on every stylesheet/script. Check p.path instead.
            low = absu.lower()
            qlow = p.query.lower()
            if (p.path.lower().endswith(_SKIP_EXT)
                    or any(p.path.startswith(pre) for pre in _SKIP_PREFIXES)
                    or any(h in low for h in _SKIP_ASSET_HINTS)
                    # asset extension carried in a query value (e.g. `/min?f=app.js`, `?file=x.css`)
                    or any(ext in qlow for ext in _SKIP_EXT)):
                continue
            links.append(absu)
        except Exception:  # noqa: BLE001
            continue
    return links


def _page_summary(page: dict) -> dict:
    """The lightweight per-page record that rides back in the result (NO html)."""
    return {k: page.get(k) for k in ("url", "http_status", "title", "content_length", "elapsed_ms")}


def _persist(on_page, page: dict, html: str) -> bool:
    """Hand one page's full HTML to the server (which saves it to S3 + DB). Best-effort: a single
    failed save is logged by the worker and must not abort the crawl. Returns True when saved."""
    if on_page is None or not html:
        return False
    try:
        return bool(on_page({**_page_summary(page), "final_url": page.get("final_url"), "html": html}))
    except Exception:  # noqa: BLE001 — worker's on_page already logs; never break the crawl
        return False


@handler("crawl_single", "crawl_single:meta", "extract_meta")
def lite_fetch(task: dict, cfg, on_progress=None, on_page=None) -> dict:
    """Fetch one page and extract title + meta, shipping its HTML to the server for persistence."""
    url = (task.get("payload") or {}).get("url")
    if not url:
        raise ValueError("task payload has no 'url'")
    page = _fetch(url, cfg, try_variants=True)
    html = page.pop("_html", "")
    saved = _persist(on_page, page, html)
    if on_progress:
        on_progress(done=1, total=1, current_url=url, title=page.get("title"))
    return {**page, "engine": "lite-fetch", "node": socket.gethostname(),
            "task_type": task.get("task_type"), "pages_saved": 1 if saved else 0}


@handler("crawl_pages")
def crawl_pages(task: dict, cfg, on_progress=None, on_page=None) -> dict:
    """Re-scrape a specific list of page URLs (payload.pages) — no BFS. Used by rescrape-failed to
    retry only the pages that previously failed, without re-fetching the good ones."""
    payload = task.get("payload") or {}
    urls = [u for u in (payload.get("pages") or []) if u]
    if not urls:
        raise ValueError("crawl_pages task has no 'pages'")
    started = time.time()
    pages: list[dict] = []
    errors = 0
    saved = 0
    total = len(urls)
    delay = _page_delay(task, cfg)
    for i, url in enumerate(urls, 1):
        if delay and i > 1:
            time.sleep(delay)
        try:
            # A repair: the URL already failed once, so try the other spellings too.
            page = _fetch(url, cfg, try_variants=True)
            html = page.pop("_html", "")
            if _persist(on_page, page, html):
                saved += 1
            pages.append(_page_summary(page))
        except Exception as exc:  # noqa: BLE001 — one bad page shouldn't abort the batch
            errors += 1
            pages.append({"url": url, "error": str(exc)})
        if on_progress:
            on_progress(done=i, total=total, current_url=url,
                        title=(pages[-1].get("title") if pages else None))
    elapsed_ms = int((time.time() - started) * 1000)
    return {
        "engine": "lite-crawl-pages",
        "node": socket.gethostname(),
        "task_type": task.get("task_type"),
        "pages_crawled": len(pages),
        "pages_saved": saved,
        "links_found": total,          # for crawl_pages, "found" = the urls we were asked to retry
        "errors": errors,
        "elapsed_ms": elapsed_ms,
        "elapsed_seconds": round(elapsed_ms / 1000, 2),
        "pages": pages,
    }


@handler("crawl_all", "crawl", "crawl_sitemap")
def full_crawl(task: dict, cfg, on_progress=None, on_page=None) -> dict:
    """BFS the whole site: crawl every same-host page up to max_pages, reporting progress per page.
    This is the 'clone the whole website' path — total wall-clock is returned as elapsed_ms."""
    payload = task.get("payload") or {}
    start_url = payload.get("url")
    if not start_url:
        raise ValueError("task payload has no 'url'")
    max_pages = int(payload.get("max_pages") or 50)
    host = urlparse(start_url).netloc

    started = time.time()
    seen: set[str] = set()
    discovered: set[str] = set()   # every distinct same-host link found (crawled or still queued)
    # (url, referer) — the referer is the page whose markup produced this link, or None for the
    # seed. It is what lets an interior fetch describe itself as a click rather than a typed URL.
    queue: list[tuple[str, str | None]] = [(_normalize_url(start_url), None)]
    discovered.add(queue[0][0])
    pages: list[dict] = []
    errors = 0
    saved = 0

    delay = _page_delay(task, cfg)
    while queue and len(pages) < max_pages:
        url, referer = queue.pop(0)
        if url in seen:
            continue
        if delay and pages:
            time.sleep(delay)
        seen.add(url)
        try:
            # Only the SEED gets the fallback ladder. Interior pages came from the site's own
            # markup, so their spelling is already the site's own and a 404 there is a real 404.
            page = _fetch(url, cfg, try_variants=not pages, referer=referer)
            html = page.pop("_html", "")
            # Ship the FULL html to the server to persist (S3 + Page row); keep the summary light.
            if _persist(on_page, page, html):
                saved += 1
            pages.append(_page_summary(page))
            found_on = page["final_url"]
            queued = {u for u, _ in queue}
            for link in _same_host_links(html, found_on, host):
                discovered.add(link)
                if link not in seen and link not in queued:
                    queue.append((link, found_on))
                    queued.add(link)
        except Exception as exc:  # noqa: BLE001 — one bad page shouldn't abort the crawl
            errors += 1
            pages.append({"url": url, "error": str(exc)})
        if on_progress:
            on_progress(done=len(pages), total=min(max_pages, len(seen) + len(queue)),
                        current_url=url, title=(pages[-1].get("title") if pages else None))

    elapsed_ms = int((time.time() - started) * 1000)
    return {
        "start_url": start_url,
        "engine": "lite-crawl-all",
        "node": socket.gethostname(),
        "task_type": task.get("task_type"),
        "pages_crawled": len(pages),
        "pages_saved": saved,              # pages whose HTML the server persisted (S3 + DB)
        "links_found": len(discovered),    # distinct same-host links discovered by the BFS
        "errors": errors,
        "elapsed_ms": elapsed_ms,          # total time to clone the site (all pages)
        "elapsed_seconds": round(elapsed_ms / 1000, 2),
        "max_pages": max_pages,
        "pages": pages,
    }


def execute(task: dict, cfg, on_progress=None, on_page=None) -> dict:
    """Dispatch a task to its handler. Raises on unknown type or handler failure (the caller turns
    that into a ``failed`` result). ``on_page`` persists each fetched page's HTML via the server."""
    task_type = task.get("task_type")
    fn = _HANDLERS.get(task_type)
    if fn is None:
        raise ValueError(f"no handler registered for task_type={task_type!r}")
    return fn(task, cfg, on_progress=on_progress, on_page=on_page)


def supported_task_types() -> list[str]:
    return sorted(_HANDLERS.keys())
