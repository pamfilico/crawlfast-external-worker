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
    resp = requests.get(url, timeout=getattr(cfg, "request_timeout_seconds", 30.0),
                        headers=_UA, allow_redirects=True)
    html = resp.text or ""
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
            absu = urljoin(base_url, href).split("#")[0].rstrip("/")
            p = urlparse(absu)
            if p.scheme in ("http", "https") and p.netloc == host and not absu.lower().endswith(_SKIP_EXT):
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
    queue: list[str] = [start_url.rstrip("/")]
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
