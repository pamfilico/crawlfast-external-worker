"""Worker configuration — loaded from a YAML file (and overridable by env vars).

The node knows NOTHING about the backend's environment. Its entire world is:
  - api_base_url : where to reach the crawlfast API (LAN IP now, cloud later)
  - api_key      : the worker's own key (identifies it — "worker A has key A")
  - poll_interval_seconds, request_timeout_seconds, worker_name (optional)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class WorkerConfig:
    api_base_url: str
    api_key: str
    worker_name: str = ""
    poll_interval_seconds: float = 5.0
    request_timeout_seconds: float = 30.0

    # ── optional transport tuning (all OFF by default) ──────────────────────
    # Every deployed node keeps its exact current behaviour unless it opts in, so these can ship
    # without touching a single running worker.
    #
    #: Reuse one TCP+TLS connection instead of dialling per request. Measured on a live node:
    #: a fresh API call costs 255-400ms, the same call on a warm connection costs 67ms — and the
    #: worker makes ~2 API calls per page plus one fetch, all of them currently cold.
    http_session: bool = False
    #: gzip the page body and POST it to /tasks/<id>/page-compressed. Measured: 171,485 -> 30,448
    #: bytes on a real page (5.6x). Only used when the SERVER advertises the feature, so pointing
    #: an opted-in worker at an older backend is safe.
    compress_pages: bool = False
    #: Override the HTTP Host header. Needed only to reach a Caddy vhost by IP — on macOS a
    #: ``*.local`` name costs ~5s in mDNS resolution per lookup, which would dwarf every timing
    #: this worker is used to measure.
    host_header: str = ""
    #: Seconds to wait between page fetches on one site. 0 = the current back-to-back behaviour.
    #: A per-task value from the server overrides this; see executor._page_delay.
    page_delay_seconds: float = 0.0

    @property
    def base(self) -> str:
        return self.api_base_url.rstrip("/")


def load_config(path: str | None = None) -> WorkerConfig:
    """Load YAML config, with env-var overrides (env wins so Docker/cron can inject secrets).

    Env overrides: CRAWLFAST_WORKER_API_BASE_URL, CRAWLFAST_WORKER_API_KEY,
    CRAWLFAST_WORKER_NAME, CRAWLFAST_WORKER_POLL_INTERVAL, CRAWLFAST_WORKER_TIMEOUT.

    Optional transport flags (all default OFF, so an existing node is byte-for-byte unchanged):
    CRAWLFAST_WORKER_HTTP_SESSION, CRAWLFAST_WORKER_COMPRESS_PAGES, CRAWLFAST_WORKER_HOST_HEADER.
    """
    data: dict = {}
    path = path or os.getenv("CRAWLFAST_WORKER_CONFIG", "config.yaml")
    if path and os.path.exists(path):
        if yaml is None:
            raise RuntimeError("pyyaml is required to read the YAML config; `pip install pyyaml`")
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    api_base_url = os.getenv("CRAWLFAST_WORKER_API_BASE_URL") or data.get("api_base_url") or ""
    api_key = os.getenv("CRAWLFAST_WORKER_API_KEY") or data.get("api_key") or ""
    worker_name = os.getenv("CRAWLFAST_WORKER_NAME") or data.get("worker_name") or ""
    poll = os.getenv("CRAWLFAST_WORKER_POLL_INTERVAL") or data.get("poll_interval_seconds") or 5.0
    timeout = os.getenv("CRAWLFAST_WORKER_TIMEOUT") or data.get("request_timeout_seconds") or 30.0

    if not api_base_url:
        raise RuntimeError("api_base_url is required (config.yaml or CRAWLFAST_WORKER_API_BASE_URL)")
    if not api_key:
        raise RuntimeError("api_key is required (config.yaml or CRAWLFAST_WORKER_API_KEY)")

    return WorkerConfig(
        api_base_url=api_base_url,
        api_key=api_key,
        worker_name=worker_name,
        poll_interval_seconds=float(poll),
        request_timeout_seconds=float(timeout),
        http_session=_flag("CRAWLFAST_WORKER_HTTP_SESSION", bool(data.get("http_session", False))),
        compress_pages=_flag("CRAWLFAST_WORKER_COMPRESS_PAGES", bool(data.get("compress_pages", False))),
        host_header=os.getenv("CRAWLFAST_WORKER_HOST_HEADER") or data.get("host_header") or "",
        page_delay_seconds=float(
            os.getenv("CRAWLFAST_WORKER_PAGE_DELAY_SECONDS")
            or data.get("page_delay_seconds")
            or 0.0
        ),
    )
