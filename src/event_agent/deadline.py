"""Общий дедлайн прогона: все паузы и повторы учитывают оставшееся время."""

from __future__ import annotations

import threading
import time


class DeadlineExceeded(Exception):  # noqa: N818 - это не ошибка, а штатное прерывание
    """Время прогона вышло."""


class Deadline:
    """Монотонный дедлайн с прерываемым сном; None-таймаут означает «без ограничения»."""

    def __init__(self, seconds: float | None) -> None:
        self._end = None if seconds is None else time.monotonic() + seconds
        self._stop = threading.Event()

    def remaining(self) -> float | None:
        """Сколько секунд осталось (не меньше 0); None — без ограничения."""
        if self._stop.is_set():
            return 0.0
        if self._end is None:
            return None
        return max(0.0, self._end - time.monotonic())

    def expired(self) -> bool:
        """Дедлайн вышел или прогон остановлен."""
        left = self.remaining()
        return left is not None and left <= 0

    def cancel(self) -> None:
        """Сообщает всем потокам, что прогон нужно сворачивать."""
        self._stop.set()

    def sleep(self, seconds: float) -> None:
        """Спит не дольше остатка времени; по выходу дедлайна бросает DeadlineExceeded."""
        left = self.remaining()
        timeout = seconds if left is None else min(seconds, left)
        self._stop.wait(timeout)
        if self.expired():
            raise DeadlineExceeded

    def clamp(self, seconds: float) -> float:
        """Ограничивает таймаут одного вызова остатком времени (минимум 1 секунда)."""
        left = self.remaining()
        return seconds if left is None else max(1.0, min(seconds, left))
