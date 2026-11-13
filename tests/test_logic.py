from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from event_agent.llm import Event, _parse_json
from event_agent.search import is_blocked, normalize_url
from event_agent.state import State, StateStore, text_hash, titles_match

NOW = datetime(2026, 11, 13, 12, 0).astimezone()


def make(title="Концерт группы Кино", day="2026-12-01", **kw) -> Event:
    return Event.model_validate({"title": title, "start_date": day, **kw})


def test_normalize_url():
    assert (
        normalize_url("HTTPS://www.Example.com:443/a/b/?utm_source=x&id=3#frag")
        == "https://example.com/a/b?id=3"
    )
    assert normalize_url("ftp://x.com/a") == ""
    assert normalize_url("https://example.com:99999/") == ""


def test_blocked_with_www_and_subdomain():
    assert is_blocked("https://www.vk.com/x", ["vk.com"])
    assert is_blocked("https://m.vk.com/x", ["vk.com"])
    assert not is_blocked("https://notvk.com/x", ["vk.com"])


def test_parse_json_repairs_inner_quotes():
    raw = (
        '```json\n{"events": [{"title": "Выставка "Черный квадрат"", '
        '"start_date": "2026-12-01"}]}\n```'
    )
    payload = _parse_json(raw)
    assert payload and payload["events"][0]["title"] == 'Выставка "Черный квадрат"'


def test_event_time_and_dates():
    ev = make(start_time="19:00–21:00", end_date="2026-11-01")
    assert ev.start_time == "19:00"
    assert ev.end_date is None  # конец раньше начала отброшен
    assert Event.model_validate(
        {"title": "x", "start_date": "2026-12-01T19:00"}
    ).start_date == date(2026, 12, 1)


def test_event_blank_fields_and_url():
    ev = make(venue="  null ", event_url="javascript:alert(1)", city=" ")
    assert ev.venue is None and ev.event_url is None and ev.city is None


def test_titles_match_guards():
    assert titles_match("концерт", "концерт") is True
    # короткое название не склеивается с длинным
    assert titles_match("концерт", "концерт группы кино в клубе") is False
    assert titles_match("концерт группы кино", "концерт группы кино в клубе") is True


def test_register_new_same_updated():
    state = State()
    first = state.register(make(), "https://a.com/1", now=NOW)
    assert first.kind == "new"
    again = state.register(make(), "https://b.com/2", now=NOW)
    assert (again.kind, again.key) == ("same", first.key)
    assert state.event_record(first.key)["sources"] == ["https://a.com/1", "https://b.com/2"]
    upd = state.register(make(start_time="19:00"), "https://c.com/3", now=NOW)
    assert upd.kind == "updated"
    cancel = state.register(make(is_cancelled=True), "https://c.com/3", now=NOW)
    assert cancel.kind == "updated"


def test_register_distinguishes_venues_and_cities():
    state = State()
    state.register(make(venue="Клуб Икра, ул. Светланская 1"), "u1", now=NOW)
    other = state.register(make(venue="Арена Приморье, Спортивная 2"), "u2", now=NOW)
    assert other.kind == "new"


def test_failure_quarantine_and_ttl():
    state = State()
    for _ in range(3):
        state.record_failure(
            "u", "article", "llm_failed", now=NOW, quarantine_after=3, quarantine_days=7
        )
    assert state.should_read_article("u", ttl_days=30, now=NOW, quarantine_after=3) == "quarantined"
    later = NOW + timedelta(days=8)
    assert state.should_read_article("u", ttl_days=30, now=later, quarantine_after=3) == "read"
    state.record_success("u", "article", "ok", now=NOW)
    assert state.should_read_article("u", ttl_days=30, now=NOW, quarantine_after=3) == "known"


def test_prune_and_roundtrip(tmp_path):
    state = State()
    state.register(make(day="2026-11-01"), "u", now=NOW)
    state.register(make(title="Другое событие тут", day="2026-12-01"), "u", now=NOW)
    state.record_success("old", "article", "ok", now=NOW - timedelta(days=40))
    events, pages = state.prune(date(2026, 11, 13), ttl_days=30, now=NOW)
    assert (events, pages) == (1, 1)
    store = StateStore(tmp_path / "s" / "state.json")
    store.save(state)
    assert len(store.load().events) == 1


def test_corrupt_state_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{oops", encoding="utf-8")
    assert StateStore(path).load().events == {}
    assert (tmp_path / "state.json.corrupt").exists()


def test_text_hash_ignores_whitespace():
    assert text_hash("a  b\n c") == text_hash("a b c")


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_title_rejected(bad):
    with pytest.raises(ValueError):
        make(title=bad)
