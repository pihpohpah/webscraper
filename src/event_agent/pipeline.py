"""Один прогон: поиск и чтение страниц идут конвейером, события дедуплицируются в конце.

Потоки: афиши стартуют сразу, а результаты каждого поискового запроса тут же превращаются в
задачи «скачать → LLM», не дожидаясь остальных запросов. Состояние в рабочих потоках только
читается; все изменения делает основной поток после завершения задач.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from event_agent.config import Config
from event_agent.deadline import Deadline, DeadlineExceeded
from event_agent.llm import Event, extract_events
from event_agent.search import (
    Fetched,
    fetch_text,
    is_blocked,
    normalize_url,
    pause_between_requests,
    search,
)
from event_agent.state import State, now_utc, text_hash

logger = logging.getLogger(__name__)


@dataclass
class Stats:
    """Счётчики и замеры одного прогона."""

    found: int = 0  # уникальных URL после фильтров
    skipped: int = 0  # уже прочитанные
    quarantined: int = 0  # в карантине после повторных неудач
    deferred: int = 0  # не вошли в лимит pages_per_run, уйдут в следующий прогон
    read: int = 0  # страниц успешно обработано LLM
    failed: int = 0  # страниц не прочитано (загрузка или LLM)
    new: int = 0
    updated: int = 0
    seconds: float = 0.0
    # Замеры. search_seconds — стенное время поиска; tail_seconds — сколько после него
    # дожимались задачи страниц (не перекрытая поиском часть); *_busy — суммарное время
    # работы всех потоков (делить на workers, чтобы понять загрузку).
    search_seconds: float = 0.0
    tail_seconds: float = 0.0
    fetch_busy_seconds: float = 0.0
    llm_busy_seconds: float = 0.0


@dataclass
class Job:
    """Страница, которая ушла бы в LLM (используется в dry-run)."""

    url: str
    page_type: str


@dataclass
class Outcome:
    """Результат прогона: события с пометкой new/updated, счётчики, признак таймаута."""

    stats: Stats = field(default_factory=Stats)
    changes: dict[str, str] = field(default_factory=dict)  # ключ события → new | updated
    timed_out: bool = False
    jobs: list[Job] = field(default_factory=list)  # заполняется только в dry-run


@dataclass
class PageResult:
    """Итог задачи по одной странице; состояние по нему обновляет основной поток."""

    url: str
    page_type: str
    # extracted | unchanged | fetch_problem | llm_failed | deferred | cancelled | would_read
    kind: str
    fetched: Fetched | None = None
    events: list[Event] | None = None
    content_hash: str | None = None
    fetch_seconds: float = 0.0
    llm_seconds: float = 0.0


class Budget:
    """Лимит страниц в LLM на прогон. Афиши резервируют места, статьи берут остаток."""

    def __init__(self, total: int, listings: int) -> None:
        self._total = total
        self._used = 0
        self._reserved = listings
        self._lock = threading.Lock()

    def has_room_for_article(self) -> bool:
        """Есть ли ещё место для статьи (без захвата)."""
        with self._lock:
            return self._used + self._reserved < self._total

    def take_article(self) -> bool:
        """Занимает место под статью."""
        with self._lock:
            if self._used + self._reserved >= self._total:
                return False
            self._used += 1
            return True

    def resolve_listing(self, *, needs_llm: bool) -> bool:
        """Афиша определилась: снимает резерв и, если нужен LLM, занимает место."""
        with self._lock:
            self._reserved = max(0, self._reserved - 1)
            if not needs_llm:
                return True
            if self._used >= self._total:
                return False
            self._used += 1
            return True


@dataclass
class Context:
    """Всё, что нужно рабочим потокам; изменяемые поля защищены lock."""

    cfg: Config
    state: State
    deadline: Deadline
    stats: Stats
    today: date
    now: datetime
    dry_run: bool
    budget: Budget
    pool: ThreadPoolExecutor
    futures: list[Future[PageResult]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    articles_submitted: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def run(
    cfg: Config,
    state: State,
    *,
    deadline: Deadline,
    dry_run: bool = False,
    general: bool | None = None,
) -> Outcome:
    """Выполняет полный цикл сбора. Состояние меняется в памяти; сохраняет вызывающий."""
    started = time.monotonic()
    now = now_utc()
    today = datetime.now(ZoneInfo(cfg.timezone)).date()
    outcome = Outcome()
    use_general = cfg.general if general is None else general
    if dry_run:
        logger.info("Dry-run mode: state is not saved, LLM is not called")
    else:
        events_gone, pages_gone = state.prune(today, ttl_days=cfg.state_ttl_days, now=now)
        logger.info("State pruned: events=%d, pages=%d", events_gone, pages_gone)

    listing_urls = _unique_urls((u for res in cfg.resources for u in res.watch_urls), cfg)
    try:
        with ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="page") as pool:
            ctx = Context(
                cfg=cfg,
                state=state,
                deadline=deadline,
                stats=outcome.stats,
                today=today,
                now=now,
                dry_run=dry_run,
                budget=Budget(cfg.pages_per_run, len(listing_urls)),
                pool=pool,
            )
            ctx.seen.update(listing_urls)
            outcome.stats.found += len(listing_urls)
            for url in listing_urls:
                ctx.futures.append(
                    pool.submit(_page_task, ctx, url, "listing", "", state.listing_hash(url))
                )
            search_started = time.monotonic()
            try:
                _search_all(_query_specs(cfg, general=use_general), ctx)
            finally:
                outcome.stats.search_seconds = round(time.monotonic() - search_started, 1)
            tail_started = time.monotonic()
            _collect(ctx, outcome)
            outcome.stats.tail_seconds = round(time.monotonic() - tail_started, 1)
    except DeadlineExceeded:
        outcome.timed_out = True
    if deadline.expired():
        outcome.timed_out = True
    outcome.stats.seconds = round(time.monotonic() - started, 1)
    _round_timings(outcome.stats)
    _log_summary(outcome, dry_run, cfg.workers)
    return outcome


# --- задачи страниц (рабочие потоки) ------------------------------------------------------


def _page_task(
    ctx: Context, url: str, page_type: str, title: str, known_hash: str | None
) -> PageResult:
    """Скачать страницу и отправить в LLM. Никогда не бросает исключений."""
    is_listing = page_type == "listing"
    try:
        if ctx.deadline.expired():
            return PageResult(url, page_type, "cancelled")
        if not is_listing:
            if not ctx.budget.has_room_for_article():
                return PageResult(url, page_type, "deferred")
            if ctx.dry_run:
                taken = ctx.budget.take_article()
                return PageResult(url, page_type, "would_read" if taken else "deferred")

        began = time.monotonic()
        fetched = fetch_text(url, ctx.deadline)
        fetch_seconds = time.monotonic() - began
        if fetched.text is None:
            if is_listing:
                ctx.budget.resolve_listing(needs_llm=False)
            return PageResult(url, page_type, "fetch_problem", fetched, fetch_seconds=fetch_seconds)

        content_hash = None
        if is_listing:
            content_hash = text_hash(fetched.text[: ctx.cfg.page_chars])
            if known_hash == content_hash:
                ctx.budget.resolve_listing(needs_llm=False)
                return PageResult(
                    url, page_type, "unchanged", fetched, content_hash=content_hash,
                    fetch_seconds=fetch_seconds,
                )  # fmt: skip
            if not ctx.budget.resolve_listing(needs_llm=True):
                return PageResult(url, page_type, "deferred", fetch_seconds=fetch_seconds)
        elif not ctx.budget.take_article():
            return PageResult(url, page_type, "deferred", fetch_seconds=fetch_seconds)

        if ctx.dry_run:
            return PageResult(url, page_type, "would_read", fetch_seconds=fetch_seconds)

        began = time.monotonic()
        events = _extract(ctx, fetched.text, url, title, is_listing=is_listing)
        llm_seconds = time.monotonic() - began
        kind = "extracted" if events is not None else "llm_failed"
        return PageResult(
            url, page_type, kind, fetched, events, content_hash, fetch_seconds, llm_seconds
        )
    except DeadlineExceeded:
        return PageResult(url, page_type, "cancelled")
    except Exception as exc:  # noqa: BLE001 - сбой одной страницы не рушит прогон
        logger.error("Page processing failed for %s: %s", url, exc)
        return PageResult(url, page_type, "llm_failed")


def _extract(
    ctx: Context, text: str, url: str, title: str, *, is_listing: bool
) -> list[Event] | None:
    cfg = ctx.cfg
    return extract_events(
        text,
        url,
        title,
        model=cfg.model,
        today=ctx.today,
        page_chars=cfg.page_chars,
        deadline=ctx.deadline,
        max_events=cfg.max_events_listing if is_listing else cfg.max_events_article,
        # Афиша — до 40 событий в ответе, такой вызов заметно длиннее карточки.
        timeout=cfg.llm_timeout_listing if is_listing else cfg.llm_timeout,
        retries=cfg.llm_retries,
        reasoning_effort=cfg.reasoning_effort,
        temperature=cfg.temperature,
        single_event=not is_listing,
    )


# --- поиск (потоки поиска + постановка задач) ----------------------------------------------


@dataclass(frozen=True)
class QuerySpec:
    """Один поисковый запрос: что искать, за какой срок, каким поиском."""

    query: str
    timelimit: str
    news: bool
    url_contains: tuple[str, ...] = ()


def _query_specs(cfg: Config, *, general: bool) -> list[QuerySpec]:
    """Список запросов в порядке приоритета: ресурсы, затем общий поиск."""
    specs = [
        QuerySpec(f"{query} site:{res.domain}", "w", False, tuple(res.url_contains))
        for res in cfg.resources
        for query in res.queries
    ]
    if not general:
        return specs
    for kind, timelimit in (("news", "d"), ("text", "w")):
        for template in cfg.general_queries.get(kind, []):
            for city in cfg.cities or [""]:
                specs.append(
                    QuerySpec(template.replace("{city}", city).strip(), timelimit, kind == "news")
                )
    return specs


def _search_all(specs: list[QuerySpec], ctx: Context) -> None:
    """Запускает запросы параллельно; результаты каждого сразу уходят в задачи страниц."""
    if not specs:
        return

    def one(spec: QuerySpec) -> None:
        try:
            rows = search(
                spec.query,
                region=ctx.cfg.region,
                timelimit=spec.timelimit,
                news=spec.news,
                deadline=ctx.deadline,
            )
            if spec.url_contains:
                rows = [
                    (u, t) for u, t in rows if any(part in u.lower() for part in spec.url_contains)
                ]
            _submit_articles(ctx, rows)  # до паузы: страницы начинают грузиться сразу
            pause_between_requests(ctx.deadline)
        except DeadlineExceeded:
            return
        except Exception as exc:  # noqa: BLE001 - один запрос не рушит прогон
            logger.warning("Search query skipped: %s (%s)", spec.query, exc)

    with ThreadPoolExecutor(max_workers=ctx.cfg.search_workers, thread_name_prefix="search") as sp:
        for _ in sp.map(one, specs):
            pass


def _submit_articles(ctx: Context, rows: list[tuple[str, str]]) -> None:
    """Фильтрует найденные URL и ставит новые статьи в очередь на чтение."""
    cfg = ctx.cfg
    for raw_url, title in rows:
        url = normalize_url(raw_url)
        if not url or is_blocked(url, cfg.blocked_domains):
            continue
        with ctx.lock:
            if url in ctx.seen:
                continue
            ctx.seen.add(url)
            ctx.stats.found += 1
            verdict = ctx.state.should_read_article(
                url, ttl_days=cfg.state_ttl_days, now=ctx.now, quarantine_after=cfg.quarantine_after
            )
            if verdict == "known":
                ctx.stats.skipped += 1
                continue
            if verdict == "quarantined":
                ctx.stats.quarantined += 1
                continue
            if ctx.articles_submitted >= cfg.pages_per_run or not ctx.budget.has_room_for_article():
                ctx.stats.deferred += 1
                continue
            ctx.articles_submitted += 1
            ctx.futures.append(ctx.pool.submit(_page_task, ctx, url, "article", title, None))


# --- сбор результатов (основной поток) -----------------------------------------------------


def _collect(ctx: Context, outcome: Outcome) -> None:
    """Ждёт задачи страниц и применяет их результаты к состоянию."""
    for future in as_completed(list(ctx.futures)):
        _apply(ctx, outcome, future.result())


def _apply(ctx: Context, outcome: Outcome, res: PageResult) -> None:
    cfg, state, stats = ctx.cfg, ctx.state, outcome.stats
    stats.fetch_busy_seconds += res.fetch_seconds
    stats.llm_busy_seconds += res.llm_seconds
    if res.kind == "cancelled":
        return  # не успели: состояние не трогаем, страница уйдёт в следующий прогон
    if res.kind == "deferred":
        stats.deferred += 1
        return
    if res.kind == "would_read":
        outcome.jobs.append(Job(res.url, res.page_type))
        return
    if res.kind == "unchanged":
        if not ctx.dry_run:
            state.touch(res.url, now=ctx.now)
        return
    if res.kind in {"fetch_problem", "llm_failed"}:
        stats.failed += 1
        if ctx.dry_run:
            return
        is_article = res.page_type == "article"
        if res.kind == "fetch_problem" and res.fetched and res.fetched.reason == "gone":
            state.record_success(res.url, res.page_type, "gone", now=ctx.now)
            return
        # Пустой текст (JS-сайт) не лечится повтором: статья сразу уходит в карантин.
        immediate = (
            res.kind == "fetch_problem"
            and res.fetched is not None
            and res.fetched.reason == "empty"
            and is_article
        )
        state.record_failure(
            res.url,
            res.page_type,
            "fetch_failed" if res.kind == "fetch_problem" else "llm_failed",
            now=ctx.now,
            quarantine_after=cfg.quarantine_after,
            quarantine_days=cfg.quarantine_days,
            quarantine=is_article,
            immediate=immediate,
        )
        return
    # extracted
    stats.read += 1
    events = res.events or []
    _register_events(state, outcome, events, res, ctx.today, ctx.now)
    state.record_success(
        res.url,
        res.page_type,
        "ok" if events else "no_events",
        content_hash=res.content_hash,
        now=ctx.now,
    )


def _register_events(
    state: State, outcome: Outcome, events: list[Event], res: PageResult, today: date, now: datetime
) -> None:
    """Регистрирует события страницы в состоянии и копит new/updated для ответа."""
    for event in events:
        if (event.end_date or event.start_date) < today:
            continue
        if res.page_type == "article" and not event.event_url:
            # Для одиночной страницы ссылка на источник и есть ссылка на событие.
            event.event_url = res.url
        change = state.register(event, res.url, now=now)
        if change.kind == "new":
            outcome.changes[change.key] = "new"
        elif change.kind == "updated" and outcome.changes.get(change.key) != "new":
            outcome.changes[change.key] = "updated"
    outcome.stats.new = sum(1 for kind in outcome.changes.values() if kind == "new")
    outcome.stats.updated = sum(1 for kind in outcome.changes.values() if kind == "updated")


# --- вспомогательное -----------------------------------------------------------------------


def _unique_urls(urls: Iterable[str], cfg: Config) -> list[str]:
    """Нормализует URL и убирает повторы и заблокированные домены."""
    seen: set[str] = set()
    unique: list[str] = []
    for raw in urls:
        url = normalize_url(raw)
        if not url or url in seen or is_blocked(url, cfg.blocked_domains):
            continue
        seen.add(url)
        unique.append(url)
    return unique


def jobs_preview(jobs: list[Job]) -> list[dict[str, Any]]:
    """Страницы, которые ушли бы в LLM (для dry-run)."""
    return [{"type": job.page_type, "url": job.url} for job in jobs]


def _round_timings(stats: Stats) -> None:
    stats.fetch_busy_seconds = round(stats.fetch_busy_seconds, 1)
    stats.llm_busy_seconds = round(stats.llm_busy_seconds, 1)


def _log_summary(outcome: Outcome, dry_run: bool, workers: int) -> None:
    """Печатает итоговые строки прогона: счётчики и замеры по фазам."""
    stats = outcome.stats
    logger.info(
        "Run summary%s: urls_found=%d, skipped_known=%d, quarantined=%d, deferred=%d, "
        "pages_read=%d, pages_failed=%d, events_new=%d, events_updated=%d, elapsed=%.1fs%s",
        " (dry-run)" if dry_run else "",
        stats.found,
        stats.skipped,
        stats.quarantined,
        stats.deferred,
        stats.read,
        stats.failed,
        stats.new,
        stats.updated,
        stats.seconds,
        " [aborted by timeout]" if outcome.timed_out else "",
    )
    logger.info(
        "Timings: search=%.1fs, after_search=%.1fs; thread busy time: fetch=%.1fs, "
        "llm=%.1fs (workers=%d)",
        stats.search_seconds,
        stats.tail_seconds,
        stats.fetch_busy_seconds,
        stats.llm_busy_seconds,
        workers,
    )
