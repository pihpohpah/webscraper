from __future__ import annotations

from pathlib import Path

import pytest

from event_agent.config import Config, load_config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Минимальный конфиг во временном каталоге."""
    path = tmp_path / "config.yaml"
    path.write_text(
        """
model: openai/test
timezone: Asia/Vladivostok
general: false
pages_per_run: 5
workers: 2
search_workers: 2
run_timeout: 30
resources:
  - domain: example.com
    queries: [концерт]
    watch_urls: [https://example.com/afisha/]
blocked_domains: [vk.com]
""",
        encoding="utf-8",
    )
    return load_config(path)
