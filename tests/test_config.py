"""Worker configuration.

A node's whole world is `api_base_url` + `api_key`. Everything else is an opt-in that must default
OFF, because these settings ship to machines that cannot be redeployed remotely — a flag that
defaults ON changes the behaviour of every laptop and Pi already in the field the moment it lands.
"""

from __future__ import annotations

import pytest

from crawlfast_external_worker.config import load_config


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return str(path)


def test_yaml_supplies_the_basics(tmp_path):
    cfg = load_config(_write(tmp_path, "api_base_url: http://api:7001\napi_key: k1\n"))
    assert cfg.api_base_url == "http://api:7001"
    assert cfg.api_key == "k1"


def test_env_beats_yaml(tmp_path, monkeypatch):
    """Docker and cron inject secrets by env; they must win over a baked-in file."""
    monkeypatch.setenv("CRAWLFAST_WORKER_API_KEY", "from-env")
    cfg = load_config(_write(tmp_path, "api_base_url: http://api:7001\napi_key: from-file\n"))
    assert cfg.api_key == "from-env"


def test_a_missing_url_or_key_fails_loudly(tmp_path, monkeypatch):
    """Better than a node that boots, heartbeats against nothing, and looks alive."""
    for name in ("CRAWLFAST_WORKER_API_BASE_URL", "CRAWLFAST_WORKER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="api_base_url"):
        load_config(_write(tmp_path, "api_key: k\n"))
    with pytest.raises(RuntimeError, match="api_key"):
        load_config(_write(tmp_path, "api_base_url: http://api\n"))


def test_base_strips_the_trailing_slash(tmp_path):
    """Paths are concatenated raw; a trailing slash makes every URL a double slash."""
    cfg = load_config(_write(tmp_path, "api_base_url: http://api:7001/\napi_key: k\n"))
    assert cfg.base == "http://api:7001"


@pytest.mark.parametrize("flag,attr", [
    ("CRAWLFAST_WORKER_HTTP_SESSION", "http_session"),
    ("CRAWLFAST_WORKER_COMPRESS_PAGES", "compress_pages"),
])
def test_transport_flags_default_off_and_opt_in(tmp_path, monkeypatch, flag, attr):
    base = "api_base_url: http://api\napi_key: k\n"
    monkeypatch.delenv(flag, raising=False)
    assert getattr(load_config(_write(tmp_path, base)), attr) is False, (
        "a transport flag that defaults ON would change every deployed node the day it merges"
    )
    monkeypatch.setenv(flag, "true")
    assert getattr(load_config(_write(tmp_path, base)), attr) is True


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("maybe", False),
])
def test_flag_parsing(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setenv("CRAWLFAST_WORKER_HTTP_SESSION", raw)
    cfg = load_config(_write(tmp_path, "api_base_url: http://api\napi_key: k\n"))
    assert cfg.http_session is expected


def test_page_delay_defaults_to_zero(tmp_path, monkeypatch):
    """0 keeps every deployed node byte-identical to how it runs today."""
    monkeypatch.delenv("CRAWLFAST_WORKER_PAGE_DELAY_SECONDS", raising=False)
    cfg = load_config(_write(tmp_path, "api_base_url: http://api\napi_key: k\n"))
    assert cfg.page_delay_seconds == 0.0


def test_a_missing_config_file_is_fine_when_the_env_is_complete(monkeypatch, tmp_path):
    """How the Docker image runs: no file at all, everything injected."""
    monkeypatch.setenv("CRAWLFAST_WORKER_API_BASE_URL", "http://api:7001")
    monkeypatch.setenv("CRAWLFAST_WORKER_API_KEY", "k")
    cfg = load_config(str(tmp_path / "does-not-exist.yaml"))
    assert cfg.base == "http://api:7001"
