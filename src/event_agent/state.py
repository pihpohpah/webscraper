"""Маленькое состояние между прогонами: JSON-файл вместо базы.

Хранит три вещи:
- страницы: что уже читали, хэш афиш, счётчик неудач и карантин;
- события: что уже отдавали вызывающему сервису (чтобы не слать повторно);
- версия формата.

Состояние трогает только поток прогона, поэтому блокировок внутри нет.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from event_agent.llm import Event

logger = logging.getLogger(__name__)

STATE_VERSION = 1
SIMILARITY_THRESHOLD = 0.85
MIN_CONTAINED_CHARS = 8
MIN_CONTAINED_RATIO = 0.5
VENUE_MISMATCH_RATIO = 0.6

# Статусы страницы, которые не требуют повторного чтения до истечения TTL.
FINAL_STATUSES = {"ok", "no_events", "gone"}
FAILED_STATUSES = {"llm_failed", "fetch_failed"}


def now_utc() -> datetime:
    """Текущее время с часовым поясом (для записи в состояние)."""
    return datetime.now().astimezone().replace(microsecond=0)


def text_hash(text: str) -> str:
    """SHA-256 нормализованного текста: пробельный шум не считается изменением афиши."""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def norm_name(value: str) -> str:
    """Нижний регистр, ё → е, без пунктуации и лишних пробелов."""
    return " ".join(re.sub(r"[^\w\s]", " ", value.lower().replace("ё", "е")).split())


def fingerprint(title: str, start_date: str, city: str) -> str:
    """SHA-1 от «название|дата начала|город» в нормализованном виде."""
    payload = f"{norm_name(title)}|{start_date}|{norm_name(city)}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - не для безопасности


@dataclass
class Change:
    """Итог регистрации события: ключ записи и что изменилось."""

    key: str
    kind: str  # new | updated | same


class State:
    """Состояние страниц и событий; сериализуется в один JSON-файл."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self.pages: dict[str, dict[str, Any]] = data.get("pages", {})
        self.events: dict[str, dict[str, Any]] = data.get("events", {})

    # --- страницы -----------------------------------------------------------------------

    def page(self, url: str) -> dict[str, Any] | None:
        """Запись о странице или None, если её ещё не видели."""
        return self.pages.get(url)

    def listing_hash(self, url: str) -> str | None:
        """Хэш афиши с прошлого успешного чтения."""
        page = self.pages.get(url)
        return page.get("hash") if page else None

    def should_read_article(
        self, url: str, *, ttl_days: int, now: datetime, quarantine_after: int
    ) -> str:
        """Решает судьбу статьи: read | known | quarantined."""
        page = self.pages.get(url)
        if page is None:
            return "read"
        if page.get("status") in FINAL_STATUSES:
            return "known"
        # Неудачная страница читается снова, пока не накопит quarantine_after неудач.
        until = _parse(page.get("until"))
        if page.get("fails", 0) >= quarantine_after and until and until > now:
            return "quarantined"
        return "read"

    def record_success(
        self,
        url: str,
        page_type: str,
        status: str,
        *,
        content_hash: str | None = None,
        now: datetime,
    ) -> None:
        """Успешное чтение: сбрасывает счётчик неудач."""
        old = self.pages.get(url, {})
        self.pages[url] = {
            "type": page_type,
            "status": status,
            "hash": content_hash or old.get("hash"),
            "first_seen": old.get("first_seen") or now.isoformat(),
            "last_checked": now.isoformat(),
            "fails": 0,
            "until": None,
        }

    def record_failure(
        self,
        url: str,
        page_type: str,
        status: str,
        *,
        now: datetime,
        quarantine_after: int,
        quarantine_days: int,
        quarantine: bool = True,
        immediate: bool = False,
    ) -> int:
        """Неудача чтения: хэш афиши сохраняется, счётчик растёт, после N — карантин.

        immediate=True — безнадёжная страница (пустой текст): карантин сразу, без повторов.
        """
        old = self.pages.get(url, {})
        fails = int(old.get("fails", 0)) + 1
        if immediate:
            fails = max(fails, quarantine_after)
        until = None
        if quarantine and fails >= quarantine_after:
            until = (now + timedelta(days=quarantine_days)).isoformat()
        self.pages[url] = {
            "type": page_type,
            "status": status,
            "hash": old.get("hash"),
            "first_seen": old.get("first_seen") or now.isoformat(),
            "last_checked": now.isoformat(),
            "fails": fails,
            "until": until,
        }
        return fails

    def touch(self, url: str, *, now: datetime) -> None:
        """Афиша не изменилась: обновляем только время проверки."""
        if url in self.pages:
            self.pages[url]["last_checked"] = now.isoformat()

    # --- события ------------------------------------------------------------------------

    def register(self, event: Event, source_url: str, *, now: datetime) -> Change:
        """Сопоставляет событие с уже отданными: new / updated / same."""
        title = " ".join(event.title.split())
        start = event.start_date.isoformat()
        base_key = fingerprint(title, start, event.city or "")
        found = self._find_match(event, base_key, start)
        if found is None:
            key = base_key
            if key in self.events:
                # Тот же отпечаток, но другая площадка/сеанс: отдельное событие.
                suffix = norm_name(f"{event.start_time or ''} {event.venue or ''}")
                key = hashlib.sha1(f"{base_key}|{suffix}".encode()).hexdigest()  # noqa: S324
            self.events[key] = {
                "event": event.model_dump(mode="json"),
                "sources": [source_url],
                "first_seen": now.isoformat(),
                "last_seen": now.isoformat(),
            }
            return Change(key, "new")
        key, record = found
        return Change(key, self._merge(record, event, source_url, now))

    def event_record(self, key: str) -> dict[str, Any]:
        """Запись события по ключу."""
        return self.events[key]

    def _find_match(
        self, event: Event, base_key: str, start: str
    ) -> tuple[str, dict[str, Any]] | None:
        """Ищет то же событие: точный отпечаток, затем похожее название в ту же дату."""
        target = norm_name(event.title)
        exact = self.events.get(base_key)
        if exact is not None and _same_session(event, exact["event"]):
            return base_key, exact
        for key, record in self.events.items():
            other = record["event"]
            if other["start_date"] != start or not _same_session(event, other):
                continue
            if titles_match(target, norm_name(other["title"])):
                return key, record
        return None

    @staticmethod
    def _merge(record: dict[str, Any], event: Event, source_url: str, now: datetime) -> str:
        """Дозаполняет запись и решает, изменилось ли событие существенно."""
        stored: dict[str, Any] = record["event"]
        incoming = event.model_dump(mode="json")
        kind = "same"
        for column in ("end_date", "city", "price", "description", "event_url", "category"):
            if column == "category":
                if stored.get(column) in (None, "other") and incoming.get(column) not in (
                    None,
                    "other",
                ):
                    stored[column] = incoming[column]
                continue
            if not stored.get(column) and incoming.get(column):
                stored[column] = incoming[column]
        if stored.get("is_free") is None and incoming.get("is_free") is not None:
            stored["is_free"] = incoming["is_free"]
        # Существенные изменения: появилась площадка, время начала, отмена.
        if not stored.get("venue") and incoming.get("venue"):
            stored["venue"] = incoming["venue"]
            kind = "updated"
        if not stored.get("start_time") and incoming.get("start_time"):
            stored["start_time"] = incoming["start_time"]
            kind = "updated"
        if incoming.get("is_cancelled") and not stored.get("is_cancelled"):
            stored["is_cancelled"] = True
            kind = "updated"
        if source_url not in record["sources"]:
            record["sources"].append(source_url)
        record["last_seen"] = now.isoformat()
        return kind

    # --- обслуживание и хранение --------------------------------------------------------

    def prune(self, today: date, *, ttl_days: int, now: datetime) -> tuple[int, int]:
        """Удаляет события, которые закончились, и страницы старше TTL."""
        yesterday = (today - timedelta(days=1)).isoformat()
        old_events = [
            key
            for key, rec in self.events.items()
            if (rec["event"].get("end_date") or rec["event"]["start_date"]) < yesterday
        ]
        for key in old_events:
            del self.events[key]
        limit = now - timedelta(days=ttl_days)
        old_pages = [
            url
            for url, page in self.pages.items()
            if (_parse(page.get("last_checked")) or now) < limit and page.get("type") != "listing"
        ]
        for url in old_pages:
            del self.pages[url]
        return len(old_events), len(old_pages)

    def to_dict(self) -> dict[str, Any]:
        """Сериализуемый вид состояния."""
        return {"version": STATE_VERSION, "pages": self.pages, "events": self.events}


class StateStore:
    """Чтение и атомарная запись состояния в JSON-файл."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> State:
        """Читает файл; битый файл откладывается в *.corrupt, начинаем с пустого."""
        if not self.path.exists():
            return State()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                raise ValueError("unknown state format")
            return State(data)
        except (OSError, ValueError) as exc:
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            logger.error("State file is corrupt (%s), moving it to %s", exc, backup)
            with contextlib.suppress(OSError):
                os.replace(self.path, backup)
            return State()

    def save(self, state: State) -> None:
        """Пишет через временный файл и os.replace: обрыв не оставит половину файла."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state.to_dict(), fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


def titles_match(target: str, other: str) -> bool:
    """Нормализованные названия — одно событие: вхождение (с защитой) или близость."""
    if not target or not other:
        return False
    if target == other:
        return True
    short, long_ = sorted((target, other), key=len)
    if (
        short in long_
        and len(short) >= MIN_CONTAINED_CHARS
        and len(short) / len(long_) >= MIN_CONTAINED_RATIO
    ):
        return True
    return SequenceMatcher(None, target, other).ratio() >= SIMILARITY_THRESHOLD


def _same_session(event: Event, other: dict[str, Any]) -> bool:
    """Не противоречат ли город, площадка и время: иначе это другой сеанс/площадка."""
    city, other_city = norm_name(event.city or ""), norm_name(other.get("city") or "")
    if city and other_city and city != other_city:
        return False
    venue, other_venue = norm_name(event.venue or ""), norm_name(other.get("venue") or "")
    if venue and other_venue and not _venues_compatible(venue, other_venue):
        return False
    return not (
        event.start_time and other.get("start_time") and event.start_time != other["start_time"]
    )


def _venues_compatible(first: str, second: str) -> bool:
    """Площадки могут совпасть: одна содержит другую или названия близки."""
    if first in second or second in first:
        return True
    return SequenceMatcher(None, first, second).ratio() >= VENUE_MISMATCH_RATIO


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
