from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from event_agent import pipeline
from event_agent.api import create_app
from event_agent.deadline import Deadline
from event_agent.llm import Event
from event_agent.search import Fetched
from event_agent.state import State

FUTURE = (date.today() + timedelta(days=10)).isoformat()
LISTING = "https://example.com/afisha"
ARTICLE = "https://example.com/e/1"


def patch_io(monkeypatch, *, llm_ok=True, calls=None):
    monkeypatch.setattr(pipeline, "pause_between_requests", lambda d: None)
    monkeypatch.setattr(
        pipeline, "search", lambda q, **kw: [(ARTICLE, "Событие"), ("https://vk.com/x", "vk")]
    )
    monkeypatch.setattr(pipeline, "fetch_text", lambda url, d: Fetched("текст " * 100))

    def fake_extract(text, url, title, **kw):
        if calls is not None:
            calls.append(url)
        if not llm_ok:
            return None
        return [Event(title=f"Событие {url[-3:]} большое", start_date=FUTURE, venue="Клуб")]

    monkeypatch.setattr(pipeline, "extract_events", fake_extract)


def test_run_dedups_and_skips_known(cfg, monkeypatch):
    calls: list[str] = []
    patch_io(monkeypatch, calls=calls)
    state = State()
    first = pipeline.run(cfg, state, deadline=Deadline(30))
    assert first.stats.new == 2 and not first.timed_out
    assert sorted(calls) == sorted([LISTING, ARTICLE])  # vk.com отфильтрован

    calls.clear()
    second = pipeline.run(cfg, state, deadline=Deadline(30))
    assert second.stats.new == 0 and second.changes == {}
    assert calls == []  # статья известна, афиша без изменений


def test_llm_failure_is_retried_then_quarantined(cfg, monkeypatch):
    calls: list[str] = []
    patch_io(monkeypatch, llm_ok=False, calls=calls)
    state = State()
    for _ in range(3):
        pipeline.run(cfg, state, deadline=Deadline(30))
    assert state.page(ARTICLE)["fails"] == 3
    calls.clear()
    out = pipeline.run(cfg, state, deadline=Deadline(30))
    assert ARTICLE not in calls and out.stats.quarantined == 1
    assert state.listing_hash(LISTING) is None  # хэш афиши не записан при сбое


def test_two_sessions_same_day_not_flapping():
    state = State()
    now = datetime.now().astimezone()
    ev = lambda t: Event(title="Спектакль Гамлет", start_date=FUTURE, start_time=t)  # noqa: E731
    assert state.register(ev("12:00"), "u", now=now).kind == "new"
    assert state.register(ev("18:00"), "u", now=now).kind == "new"
    assert state.register(ev("12:00"), "u2", now=now).kind == "same"
    assert state.register(ev("18:00"), "u2", now=now).kind == "same"


def test_expired_deadline_returns_partial(cfg, monkeypatch):
    patch_io(monkeypatch)
    deadline = Deadline(0)
    out = pipeline.run(cfg, State(), deadline=deadline)
    assert out.timed_out


def test_api_flow(cfg, monkeypatch):
    patch_io(monkeypatch)
    gate = threading.Event()
    monkeypatch.setattr(pipeline, "pause_between_requests", lambda d: gate.wait(5))
    with TestClient(create_app(cfg)) as client:
        first = client.post("/runs")
        assert first.status_code == 202
        run_id = first.json()["run_id"]
        again = client.post("/runs")  # прогон заблокирован gate и ещё идёт
        assert again.json()["run_id"] == run_id and again.json()["already_running"]
        assert client.get("/health").json()["active_run_id"] == run_id
        gate.set()
        for _ in range(100):
            body = client.get(f"/runs/{run_id}").json()
            if body["status"] != "running":
                break
            time.sleep(0.05)
        assert body["status"] == "completed"
        assert body["stats"]["new"] == 2
        assert {e["change"] for e in body["events"]} == {"new"}
        assert client.get("/runs/nope").status_code == 404
        assert client.get("/health").json()["active_run_id"] is None
        # второй прогон: ничего нового, состояние пережило файл
        run2 = client.post("/runs").json()["run_id"]
        for _ in range(100):
            body2 = client.get(f"/runs/{run2}").json()
            if body2["status"] != "running":
                break
            time.sleep(0.05)
        assert body2["events"] == []
    assert cfg.state_path.exists()


def test_empty_page_goes_to_quarantine_immediately(cfg, monkeypatch):
    patch_io(monkeypatch)
    monkeypatch.setattr(
        pipeline,
        "fetch_text",
        lambda url, d: Fetched(None, "empty") if url == ARTICLE else Fetched("текст " * 100),
    )
    state = State()
    pipeline.run(cfg, state, deadline=Deadline(30))
    page = state.page(ARTICLE)
    assert page["status"] == "fetch_failed" and page["until"] is not None
    again = pipeline.run(cfg, state, deadline=Deadline(30))
    assert again.stats.quarantined == 1


def test_articles_start_before_search_finishes(cfg, monkeypatch):
    """Страница из первого запроса должна уйти в LLM, пока второй запрос ещё идёт."""
    patch_io(monkeypatch)
    cfg = cfg.__class__(**{**vars(cfg), "resources": cfg.resources * 1, "search_workers": 2})
    from event_agent.config import Resource

    cfg = cfg.__class__(**{**vars(cfg), "resources": [Resource("example.com", queries=["a", "b"])]})
    extracted = threading.Event()
    seen_during_search: list[bool] = []

    def fake_search(q, **kw):
        if q.startswith("a"):
            return [(ARTICLE, "x")]
        seen_during_search.append(extracted.wait(5))  # ждём, пока первая статья дойдёт до LLM
        return []

    def fake_extract(text, url, title, **kw):
        extracted.set()
        return []

    monkeypatch.setattr(pipeline, "search", fake_search)
    monkeypatch.setattr(pipeline, "extract_events", fake_extract)
    pipeline.run(cfg, State(), deadline=Deadline(30))
    assert seen_during_search == [True]


def test_budget_limits_llm_pages(cfg, monkeypatch):
    calls: list[str] = []
    patch_io(monkeypatch, calls=calls)
    many = [(f"https://example.com/e/{i:03d}", "t") for i in range(20)]
    monkeypatch.setattr(pipeline, "search", lambda q, **kw: many)
    cfg = cfg.__class__(**{**vars(cfg), "pages_per_run": 4})
    out = pipeline.run(cfg, State(), deadline=Deadline(30))
    assert len(calls) == 4  # лимит 4 = 1 афиша + 3 статьи
    assert LISTING in calls
    assert out.stats.deferred >= 1
    assert out.stats.llm_busy_seconds >= 0 and out.stats.search_seconds >= 0
