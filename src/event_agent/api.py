"""HTTP-API: ручка запуска прогона и получения результата."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from event_agent.config import Config
from event_agent.runner import ExternalLockError, Runner
from event_agent.schemas import HealthOut, RunOut


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

    @app.get("/health", response_model=HealthOut)
    def health() -> HealthOut:
        """Проверка живости и id активного прогона."""
        return HealthOut(active_run_id=active_runner.active_run_id())

    return app
