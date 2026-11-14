"""HTTP-API: ручка запуска прогона и получения результата."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException

from event_agent.config import Config
from event_agent.runner import ExternalLockError, Runner
from event_agent.schemas import HealthOut, RunOut
from event_agent.state import STATE_VERSION, State

logger = logging.getLogger(__name__)


def create_app(cfg: Config, runner: Runner | None = None) -> FastAPI:
    """Собирает FastAPI-приложение вокруг Runner."""
    active_runner = runner or Runner(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        active_runner.shutdown()

    app = FastAPI(title="event-agent", version="0.2.0", lifespan=lifespan)

    @app.post("/runs", response_model=RunOut, status_code=202)
    def start_run() -> RunOut:
        """Запускает прогон. Если он уже идёт — возвращает его (already_running=true)."""
        try:
            return active_runner.start()
        except ExternalLockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/runs/{run_id}", response_model=RunOut)
    def get_run(run_id: str) -> RunOut:
        """Статус и результат прогона; events заполняются по завершении."""
        record = active_runner.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return record

    @app.get("/state")
    def get_state() -> dict[str, Any]:
        """Содержимое файла состояния: страницы и уже отданные события.

        Файл пишется атомарно (os.replace) только в конце прогона, поэтому чтение во время
        прогона безопасно и отдаёт состояние на конец предыдущего.
        """
        try:
            data = json.loads(cfg.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return State().to_dict()
        except (OSError, ValueError) as exc:
            logger.warning("Cannot read state file %s", cfg.state_path, exc_info=exc)
            raise HTTPException(status_code=503, detail="State file is unreadable") from exc
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            logger.warning("State file %s has unknown format", cfg.state_path)
            raise HTTPException(status_code=503, detail="State file has unknown format")
        return data

    @app.get("/health", response_model=HealthOut)
    def health() -> HealthOut:
        """Проверка живости и id активного прогона."""
        return HealthOut(active_run_id=active_runner.active_run_id())

    return app
