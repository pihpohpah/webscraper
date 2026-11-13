"""Загрузка config.yaml в простые dataclass-объекты."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Конфиг не читается или содержит недопустимые значения."""


@dataclass(frozen=True)
class Resource:
    """Источник анонсов: домен, поисковые запросы и прямые афиши."""

    domain: str
    queries: list[str] = field(default_factory=list)
    watch_urls: list[str] = field(default_factory=list)
    url_contains: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    """Все настройки сервиса; пути уже приведены к абсолютным."""

    model: str
    reasoning_effort: str | None
    temperature: float | None
    llm_timeout: float
    llm_timeout_listing: float
    llm_retries: int
    timezone: str
    region: str
    cities: list[str]
    resources: list[Resource]
    general_queries: dict[str, list[str]]
    general: bool
    pages_per_run: int
    page_chars: int
    max_events_listing: int
    max_events_article: int
    workers: int
    search_workers: int
    blocked_domains: list[str]
    run_timeout: float
    state_path: Path
    state_ttl_days: int
    quarantine_after: int
    quarantine_days: int
    history_size: int
    log_path: Path
    lock_path: Path
    host: str
    port: int
    project_root: Path


def load_config(path: str | Path = "config.yaml") -> Config:
    """Читает YAML-конфиг; относительные пути считаются от каталога конфига."""
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open(encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}
    except OSError as exc:
        raise ConfigError(f"Cannot read config {config_path}: {exc}") from exc
    if not raw.get("model"):
        raise ConfigError(f"Required parameter 'model' is missing in {config_path}")
    root = config_path.parent
    server: dict[str, Any] = raw.get("server") or {}
    return Config(
        model=str(raw["model"]),
        reasoning_effort=raw.get("reasoning_effort"),
        temperature=raw.get("temperature"),
        llm_timeout=float(raw.get("llm_timeout", 90)),
        llm_timeout_listing=float(raw.get("llm_timeout_listing", 240)),
        llm_retries=max(0, int(raw.get("llm_retries", 1))),
        timezone=str(raw.get("timezone", "Europe/Moscow")),
        region=str(raw.get("region", "ru-ru")),
        cities=[str(city) for city in raw.get("cities") or []],
        resources=[
            Resource(
                domain=str(item["domain"]).lower(),
                queries=[str(query) for query in item.get("queries") or []],
                watch_urls=[str(url) for url in item.get("watch_urls") or []],
                url_contains=[str(part).lower() for part in item.get("url_contains") or []],
            )
            for item in raw.get("resources") or []
        ],
        general_queries={
            str(kind): [str(query) for query in queries or []]
            for kind, queries in (raw.get("general_queries") or {}).items()
        },
        general=bool(raw.get("general", True)),
        pages_per_run=max(1, int(raw.get("pages_per_run", 25))),
        page_chars=max(500, int(raw.get("page_chars", 12000))),
        max_events_listing=max(1, int(raw.get("max_events_listing", 40))),
        max_events_article=max(1, int(raw.get("max_events_article", 10))),
        workers=max(1, int(raw.get("workers", 6))),
        search_workers=max(1, int(raw.get("search_workers", 3))),
        blocked_domains=[str(domain).lower() for domain in raw.get("blocked_domains") or []],
        run_timeout=float(raw.get("run_timeout", 900)),
        state_path=_resolve(root, raw.get("state_path", "data/state.json")),
        state_ttl_days=max(1, int(raw.get("state_ttl_days", 30))),
        quarantine_after=max(1, int(raw.get("quarantine_after", 3))),
        quarantine_days=max(1, int(raw.get("quarantine_days", 7))),
        history_size=max(1, int(raw.get("history_size", 20))),
        log_path=_resolve(root, raw.get("log_path", "logs/event-agent.log")),
        lock_path=_resolve(root, raw.get("lock_path", "logs/event-agent.lock")),
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8000)),
        project_root=root,
    )


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path
