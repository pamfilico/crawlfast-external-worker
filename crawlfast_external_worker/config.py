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


@dataclass
class WorkerConfig:
    api_base_url: str
    api_key: str
    worker_name: str = ""
    poll_interval_seconds: float = 5.0
    request_timeout_seconds: float = 30.0

    @property
    def base(self) -> str:
        return self.api_base_url.rstrip("/")


def load_config(path: str | None = None) -> WorkerConfig:
    """Load YAML config, with env-var overrides (env wins so Docker/cron can inject secrets).

    Env overrides: CRAWLFAST_WORKER_API_BASE_URL, CRAWLFAST_WORKER_API_KEY,
    CRAWLFAST_WORKER_NAME, CRAWLFAST_WORKER_POLL_INTERVAL, CRAWLFAST_WORKER_TIMEOUT.
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
    )
