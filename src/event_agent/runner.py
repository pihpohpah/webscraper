"""Запуск прогонов в фоне: один активный прогон, история последних N в памяти."""

from __future__ import annotations

import contextlib
import fcntl
import logging
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import TextIO

from event_agent.config import Config
from event_agent.deadline import Deadline
from event_agent.pipeline import Outcome, run
from event_agent.schemas import EventOut, RunOut, RunStats
from event_agent.state import State, StateStore, now_utc

logger = logging.getLogger(__name__)


class ExternalLockError(RuntimeError):
    """Состояние занято другим процессом (например, CLI-прогоном)."""


def acquire_lock(lock_path: Path) -> TextIO | None:
    """Берёт неблокирующую блокировку flock; None — если уже занято."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def build_events(state: State, outcome: Outcome) -> list[EventOut]:
    """Собирает события ответа по ключам изменений: сначала new, затем updated."""
    items: list[EventOut] = []
    for key, change in outcome.changes.items():
        record = state.events.get(key)
        if record is None:
            continue
        items.append(
            EventOut.model_validate(
                {**record["event"], "id": key, "change": change, "source_urls": record["sources"]}
            )
        )
    items.sort(key=lambda e: (e.change != "new", e.start_date, e.start_time or "", e.title))
    return items


class Runner:
    """Единственный активный прогон + история завершённых."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.store = StateStore(cfg.state_path)
        self._guard = threading.Lock()
        self._runs: OrderedDict[str, RunOut] = OrderedDict()
        self._active: str | None = None
        self._deadline: Deadline | None = None
        self._thread: threading.Thread | None = None

    # --- публичный интерфейс ------------------------------------------------------------

    def active_run_id(self) -> str | None:
        """Идентификатор идущего прогона."""
        with self._guard:
            return self._active

    def get(self, run_id: str) -> RunOut | None:
        """Копия состояния прогона (безопасна для сериализации в другом потоке)."""
        with self._guard:
            run_state = self._runs.get(run_id)
            return run_state.model_copy(deep=True) if run_state else None

    def start(self) -> RunOut:
        """Запускает прогон; если уже идёт — возвращает идущий с already_running=True."""
        with self._guard:
            if self._active:
                current = self._runs[self._active].model_copy(deep=True)
                current.already_running = True
                return current
            lock = acquire_lock(self.cfg.lock_path)
            if lock is None:
                raise ExternalLockError("Another event-agent process is already running")
            run_id = uuid.uuid4().hex[:12]
            record = RunOut(run_id=run_id, status="running", started_at=now_utc())
            self._runs[run_id] = record
            self._trim()
            self._active = run_id
            self._deadline = Deadline(self.cfg.run_timeout)
            self._thread = threading.Thread(
                target=self._execute,
                args=(run_id, lock, self._deadline),
                name=f"run-{run_id}",
                daemon=True,
            )
            self._thread.start()
            return record.model_copy(deep=True)

    def shutdown(self, wait_seconds: float = 20) -> None:
        """Просит активный прогон свернуться и ждёт; состояние успевает сохраниться."""
        with self._guard:
            deadline, thread = self._deadline, self._thread
        if deadline and thread and thread.is_alive():
            logger.info("Service shutdown: stopping the active run")
            deadline.cancel()
            thread.join(wait_seconds)

    # --- внутреннее ---------------------------------------------------------------------

    def _execute(self, run_id: str, lock: TextIO, deadline: Deadline) -> None:
        """Тело потока прогона: загрузка состояния → прогон → сохранение → результат."""
        update: dict[str, object]
        try:
            state = self.store.load()
            outcome = run(self.cfg, state, deadline=deadline)
            self.store.save(state)  # сохраняем и частичный результат при таймауте
            update = {
                "status": "partial" if outcome.timed_out else "completed",
                "timed_out": outcome.timed_out,
                "stats": RunStats(**vars(outcome.stats)),
                "events": build_events(state, outcome),
            }
        except Exception as exc:  # noqa: BLE001 - любой сбой фиксируем в результате прогона
            logger.exception("Run %s failed", run_id)
            update = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            with contextlib.suppress(OSError):
                lock.close()
        with self._guard:
            record = self._runs[run_id]
            for field_name, value in update.items():
                setattr(record, field_name, value)
            record.finished_at = now_utc()
            self._active = None
            self._deadline = None

    def _trim(self) -> None:
        """Оставляет последние history_size прогонов, не трогая активный."""
        while len(self._runs) > self.cfg.history_size:
            oldest = next(iter(self._runs))
            if oldest == self._active:
                break
            del self._runs[oldest]
