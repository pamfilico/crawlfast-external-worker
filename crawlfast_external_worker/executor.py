"""Task executor — turns a claimed task into a result dict.

v1 is deliberately LIGHTWEIGHT: it proves the whole pull→do→report loop without a browser.
For crawl/metadata task types it does a real (but minimal) HTTP fetch and pulls out the page
title + a few meta tags. This is the "simple test before we go into scraping" the plumbing needs.

To reach full parity with the internal crawl worker later, register a heavier handler here that
either (a) runs the scraper's Playwright stack locally, or (b) calls the crawl-api — same
signature, same registry. Nothing else in the worker changes.
"""

from __future__ import annotations

import re
import socket
import time

import requests

# task_type strings mirror the backend/scraper vocabulary.
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)

_HANDLERS: dict[str, callable] = {}


def handler(*task_types: str):
    def deco(fn):
        for t in task_types:
            _HANDLERS[t] = fn
        return fn

    return deco


def _extract_meta(html: str, limit: int = 15) -> dict:
    out = {}
    for name, content in _META_RE.findall(html or ""):
        if name.lower() in ("description", "keywords", "og:title", "og:description", "og:site_name"):
            out[name.lower()] = content[:500]
        if len(out) >= limit:
            break
    return out


@handler("crawl_single", "crawl", "crawl_all", "crawl_sitemap", "extract_meta", "crawl_single:meta")
def lite_fetch(task: dict, cfg) -> dict:
    """Fetch the URL and extract title + meta (no browser). Real, minimal, verifiable."""
    payload = task.get("payload") or {}
    url = payload.get("url")
    if not url:
        raise ValueError("task payload has no 'url'")
    started = time.time()
    resp = requests.get(
        url,
        timeout=getattr(cfg, "request_timeout_seconds", 30.0),
        headers={"User-Agent": "crawlfast-external-worker/0.1 (+lite-fetch)"},
        allow_redirects=True,
    )
    html = resp.text or ""
    title_match = _TITLE_RE.search(html)
    return {
        "url": url,
        "final_url": resp.url,
        "http_status": resp.status_code,
        "elapsed_ms": int((time.time() - started) * 1000),
        "content_length": len(html),
        "title": (title_match.group(1).strip()[:300] if title_match else None),
        "meta": _extract_meta(html),
        "engine": "lite-fetch",
        "node": socket.gethostname(),
        "task_type": task.get("task_type"),
    }


def execute(task: dict, cfg) -> dict:
    """Dispatch a task to its handler. Raises on unknown type or handler failure (the caller
    turns that into a ``failed`` result)."""
    task_type = task.get("task_type")
    fn = _HANDLERS.get(task_type)
    if fn is None:
        raise ValueError(f"no handler registered for task_type={task_type!r}")
    return fn(task, cfg)


def supported_task_types() -> list[str]:
    return sorted(_HANDLERS.keys())
