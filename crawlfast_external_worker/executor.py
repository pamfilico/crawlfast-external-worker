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
"""

from __future__ import annotations

import re
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_HREF_RE = re.compile(r'href=["\']([^"\'#]+)["\']', re.IGNORECASE)
_UA = {"User-Agent": "crawlfast-external-worker/0.2 (+node-crawl)"}
_SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".mp4",
             ".css", ".js", ".ico", ".xml", ".json", ".woff", ".woff2", ".ttf")
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


def _fetch(url: str, cfg) -> dict:
    started = time.time()
    # 12s default (was 30) so a throttling/slow site can't hold a worker hostage for the full crawl.
    timeout = getattr(cfg, "request_timeout_seconds", 12.0)
    resp = requests.get(url, timeout=timeout, headers=_UA, allow_redirects=True, stream=True)
    # Only read/parse HTML. A non-HTML response (asset, PDF, binary) that slipped through has no
    # pages to follow — skip the body so we don't download megabytes or extract junk links.
    ctype = (resp.headers.get("Content-Type") or "").lower()
    is_html = "html" in ctype or ctype == ""
    html = ""
    if is_html:
        # Cap the body (~3MB) so a pathological page can't blow up memory/time.
        chunks, size = [], 0
        for chunk in resp.iter_content(chunk_size=65536, decode_unicode=True):
            if not chunk:
                continue
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "ignore"))
            size += len(chunks[-1])
            if size > 3_000_000:
                break
        html = "".join(chunks)
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


def _same_host_links(html: str, base_url: str, host: str) -> list[str]:
    links = []
    for href in _HREF_RE.findall(html or ""):
        try:
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


@handler("crawl_single", "crawl_single:meta", "extract_meta")
def lite_fetch(task: dict, cfg, on_progress=None) -> dict:
    """Fetch one page and extract title + meta."""
    url = (task.get("payload") or {}).get("url")
    if not url:
        raise ValueError("task payload has no 'url'")
    page = _fetch(url, cfg)
    page.pop("_html", None)
    if on_progress:
        on_progress(done=1, total=1, current_url=url, title=page.get("title"))
    return {**page, "engine": "lite-fetch", "node": socket.gethostname(), "task_type": task.get("task_type")}


@handler("crawl_all", "crawl", "crawl_sitemap")
def full_crawl(task: dict, cfg, on_progress=None) -> dict:
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
    queue: list[str] = [_normalize_url(start_url)]
    pages: list[dict] = []
    errors = 0

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            page = _fetch(url, cfg)
            html = page.pop("_html", "")
            pages.append({k: page[k] for k in ("url", "http_status", "title", "content_length", "elapsed_ms")})
            for link in _same_host_links(html, page["final_url"], host):
                if link not in seen and link not in queue:
                    queue.append(link)
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
        "errors": errors,
        "elapsed_ms": elapsed_ms,          # total time to clone the site (all pages)
        "elapsed_seconds": round(elapsed_ms / 1000, 2),
        "max_pages": max_pages,
        "pages": pages,
    }


def execute(task: dict, cfg, on_progress=None) -> dict:
    """Dispatch a task to its handler. Raises on unknown type or handler failure (the caller turns
    that into a ``failed`` result)."""
    task_type = task.get("task_type")
    fn = _HANDLERS.get(task_type)
    if fn is None:
        raise ValueError(f"no handler registered for task_type={task_type!r}")
    return fn(task, cfg, on_progress=on_progress)


def supported_task_types() -> list[str]:
    return sorted(_HANDLERS.keys())
