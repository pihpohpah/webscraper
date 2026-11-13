"""Контракт HTTP-API: модели ответа."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from event_agent.llm import Category

RunStatus = Literal["running", "completed", "partial", "failed"]


class EventOut(BaseModel):
    """Событие в ответе. `change`: new — ещё не отдавали, updated — изменилось существенно."""

    id: str = Field(description="Стабильный ключ события (SHA-1 названия, даты и города)")
    change: Literal["new", "updated"]
    title: str
    category: Category
    start_date: date
    end_date: date | None = None
    start_time: str | None = Field(default=None, description="HH:MM")
    city: str | None = None
    venue: str | None = None
    price: str | None = None
    is_free: bool | None = None
    description: str | None = None
    event_url: str | None = None
    is_cancelled: bool = False
    source_urls: list[str] = Field(default_factory=list)


class RunStats(BaseModel):
    """Счётчики прогона."""

    found: int = 0
    skipped: int = 0
    quarantined: int = 0
    deferred: int = 0
    read: int = 0
    failed: int = 0
    new: int = 0
    updated: int = 0
    seconds: float = 0.0
    search_seconds: float = 0.0
    tail_seconds: float = 0.0
    fetch_busy_seconds: float = 0.0
    llm_busy_seconds: float = 0.0


class RunOut(BaseModel):
    """Состояние и результат прогона."""

    run_id: str
    status: RunStatus
    already_running: bool = Field(
        default=False, description="true — запрос не создал прогон, а вернул уже идущий"
    )
    started_at: datetime
    finished_at: datetime | None = None
    timed_out: bool = False
    error: str | None = None
    stats: RunStats = Field(default_factory=RunStats)
    events: list[EventOut] = Field(default_factory=list)


class HealthOut(BaseModel):
    """Состояние сервиса."""

    status: Literal["ok"] = "ok"
    active_run_id: str | None = None
