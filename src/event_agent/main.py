"""CLI: запуск HTTP-сервиса или разовый прогон для отладки."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from event_agent.config import Config, ConfigError, load_config
from event_agent.deadline import Deadline
from event_agent.pipeline import jobs_preview, run
from event_agent.runner import acquire_lock, build_events
from event_agent.state import StateStore

NOISY_LOGGERS = ("httpx", "litellm", "LiteLLM", "primp", "ddgs")


def main(argv: list[str] | None = None) -> int:
    """Точка входа: `serve` (по умолчанию) или `run` для разового прогона."""
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    setup_logging(cfg.log_path)
    if args.command == "run":
        return cmd_run(cfg, general=args.general, dry_run=args.dry_run)
    return cmd_serve(cfg, host=args.host, port=args.port)


def setup_logging(log_path: Path) -> None:
    """Пишет логи в файл и в консоль, шумные библиотеки — на WARNING."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler], force=True)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def cmd_serve(cfg: Config, *, host: str | None, port: int | None) -> int:
    """Поднимает HTTP-сервис (один воркер: активный прогон хранится в памяти процесса)."""
    import uvicorn  # noqa: PLC0415 - тяжёлый импорт нужен только для serve

    from event_agent.api import create_app  # noqa: PLC0415

    uvicorn.run(
        create_app(cfg),
        host=host or cfg.host,
        port=port or cfg.port,
        log_config=None,  # логи уже настроены setup_logging
    )
    return 0


def cmd_run(cfg: Config, *, general: bool, dry_run: bool) -> int:
    """Разовый прогон из консоли; печатает JSON с найденным (для отладки)."""
    lock = acquire_lock(cfg.lock_path)
    if lock is None:
        logging.warning("Another run is still active, exiting")
        return 1
    try:
        store = StateStore(cfg.state_path)
        state = store.load()  # при dry-run только читаем, не сохраняем
        deadline = Deadline(cfg.run_timeout)
        outcome = run(cfg, state, deadline=deadline, dry_run=dry_run, general=general or None)
        if dry_run:
            print(json.dumps(jobs_preview(outcome.jobs), ensure_ascii=False, indent=2))
            return 0
        store.save(state)
        events = [e.model_dump(mode="json") for e in build_events(state, outcome)]
        print(json.dumps(events, ensure_ascii=False, indent=2))
        return 0
    finally:
        lock.close()
        logging.shutdown()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="event-agent", description="Агент поиска анонсов событий")
    parser.add_argument("--config", default="config.yaml", help="путь к config.yaml")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="запустить HTTP-сервис (по умолчанию)")
    serve.add_argument("--host", default=None, help="адрес (по умолчанию из config.yaml)")
    serve.add_argument("--port", type=int, default=None, help="порт (по умолчанию из config.yaml)")
    once = sub.add_parser("run", help="один прогон из консоли, результат — JSON в stdout")
    once.add_argument("--general", action="store_true", help="принудительно включить общий поиск")
    once.add_argument(
        "--dry-run", action="store_true", help="только показать URL, без LLM и записи состояния"
    )
    parser.set_defaults(command="serve", host=None, port=None)
    return parser


if __name__ == "__main__":
    sys.exit(main())
