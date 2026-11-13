"""Поиск через ddgs, загрузка текста страниц и нормализация URL."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from event_agent.deadline import Deadline, DeadlineExceeded

logger = logging.getLogger(__name__)

MIN_PAGE_CHARS = 200
RETRY_DELAY_SECONDS = 30
PAUSE_SECONDS = (2, 5)
TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "_openstat", "ysclid"}
TRACKING_PREFIXES = ("utm_",)
DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class Fetched:
    """Результат загрузки страницы: текст либо причина неудачи."""

    text: str | None
    # ok | gone (410, навсегда) | empty (JS/пусто) | error (временная ошибка)
    reason: str = "ok"


def normalize_url(url: str) -> str:
    """Приводит URL к виду для сравнения: без www, якорей и трекинговых параметров."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    if scheme not in DEFAULT_PORTS:
        return ""
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    if port and port != DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith(TRACKING_PREFIXES)
    ]
    return urlunsplit((scheme, host, parts.path.rstrip("/"), urlencode(query), ""))


def is_blocked(url: str, blocked_domains: Sequence[str]) -> bool:
    """Проверяет хост и все его поддомены против списка заблокированных доменов."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return any(host == domain or host.endswith(f".{domain}") for domain in blocked_domains)


def search(
    query: str,
    *,
    region: str,
    timelimit: str,
    deadline: Deadline,
    news: bool = False,
    max_results: int = 10,
) -> list[tuple[str, str]]:
    """Ищет по запросу и возвращает пары (url, title); сбои не прерывают прогон."""

    def call() -> list[tuple[str, str]]:
        client = DDGS(timeout=int(deadline.clamp(15)))
        finder = client.news if news else client.text
        rows: list[dict[str, Any]] = finder(
            query, region=region, timelimit=timelimit, max_results=max_results
        )
        return [
            (str(row.get("url") or row.get("href") or ""), str(row.get("title") or ""))
            for row in rows
        ]

    try:
        rows = _with_retry(call, deadline)
    except DeadlineExceeded:
        raise
    except (RatelimitException, TimeoutException) as exc:
        logger.warning("Search query skipped after retry: %s (%s)", query, exc)
        return []
    except DDGSException as exc:
        logger.info("Search returned no results: %s (%s)", query, exc)
        return []
    return [(url, title) for url, title in rows if url]


def fetch_text(url: str, deadline: Deadline) -> Fetched:
    """Загружает страницу как markdown; text=None и причина — если прочитать не удалось."""

    def call() -> str:
        data = DDGS(timeout=int(deadline.clamp(20))).extract(url, fmt="text_markdown")
        content = data.get("content") or ""
        return content.decode("utf-8", "ignore") if isinstance(content, bytes) else str(content)

    try:
        text = _with_retry(call, deadline).strip()
    except DeadlineExceeded:
        raise
    except (RatelimitException, TimeoutException) as exc:
        logger.warning("Page not fetched after retry: %s (%s)", url, exc)
        return Fetched(None, "error")
    except DDGSException as exc:
        if "HTTP 410" in str(exc):
            # 410 Gone — событие снято с сайта. Повторять нечего, это не сбой загрузки.
            logger.info("Listing removed from the site (410 Gone): %s", url)
            return Fetched(None, "gone")
        logger.warning("Page not fetched: %s (%s)", url, exc)
        return Fetched(None, "error")
    except Exception as exc:  # noqa: BLE001 - неожиданный сбой парсера не рушит прогон
        logger.warning("Page not fetched (unexpected error): %s (%s)", url, exc)
        return Fetched(None, "error")
    if len(text) < MIN_PAGE_CHARS:
        logger.warning("Page text too short (%d chars): %s", len(text), url)
        return Fetched(None, "empty")
    return Fetched(text)


def pause_between_requests(deadline: Deadline) -> None:
    """Спит 2–5 секунд, чтобы не упираться в лимиты поисковиков."""
    deadline.sleep(random.uniform(*PAUSE_SECONDS))


def _with_retry[T](call: Callable[[], T], deadline: Deadline) -> T:
    """Повторяет вызов один раз через 30 секунд при лимите или таймауте."""
    if deadline.expired():
        raise DeadlineExceeded
    try:
        return call()
    except (RatelimitException, TimeoutException) as exc:
        logger.warning("Transient error (%s), retrying in %ds", exc, RETRY_DELAY_SECONDS)
        deadline.sleep(RETRY_DELAY_SECONDS)
        return call()
