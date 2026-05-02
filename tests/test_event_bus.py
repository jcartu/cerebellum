from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest

from cerebellum.event_bus import EventBus


@pytest.fixture
def event_bus(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "sqlite": {"events_db": str(tmp_path / "events.sqlite3")},
                "nats": {"host": "127.0.0.1", "port": 4222, "jetstream_domain": ""},
            }
        )
    )
    bus = EventBus(config_path)
    try:
        yield bus
    finally:
        bus.close()


def test_emit_persists_event_and_defaults_context(event_bus):
    event_id = event_bus.emit("build.started", {"project": "alpha"})

    events = event_bus.query(limit=10)

    assert [event["id"] for event in events] == [event_id]
    assert events[0]["type"] == "build.started"
    assert events[0]["payload"] == {"project": "alpha"}
    assert events[0]["actor"] == "system"
    assert events[0]["context"] == {}


def test_query_filters_by_type_and_since(event_bus):
    older_id = event_bus.emit("build.started", {"step": 1}, actor="ci")
    time.sleep(0.02)
    since = datetime.now(UTC)
    time.sleep(0.02)
    newer_id = event_bus.emit("build.finished", {"step": 2}, actor="ci")

    matching = event_bus.query(types=["build.finished"], since=since, limit=10)
    all_events = event_bus.query(limit=10)

    assert [event["id"] for event in matching] == [newer_id]
    assert [event["id"] for event in all_events] == [newer_id, older_id]


def test_subscribe_warns_and_skips_when_nats_unavailable(event_bus, caplog):
    received: list[dict[str, object]] = []

    with caplog.at_level("WARNING"):
        event_bus.subscribe(received.append)

    assert received == []
    assert "NATS unavailable; subscription skipped" in caplog.text


def test_write_event_validates_required_keys(event_bus):
    """write_event rejects events missing required keys."""
    with pytest.raises(ValueError, match="missing required keys"):
        event_bus.write_event({"id": "x"})


def test_write_event_stores_prebuilt_event(event_bus):
    """write_event persists a fully-formed event dict."""
    event_bus.write_event({
        "id": "prebuilt-1",
        "timestamp": "2025-01-01T00:00:00+00:00",
        "type": "test.event",
        "payload": {"key": "val"},
        "actor": "tester",
        "context": {"run": 1},
    })
    events = event_bus.query(limit=10)
    assert any(e["id"] == "prebuilt-1" for e in events)


def test_write_event_normalizes_non_dict_payload(event_bus):
    """write_event coerces non-dict payload/context to empty dict."""
    event_bus.write_event({
        "id": "norm-1",
        "timestamp": "2025-01-01T00:00:00+00:00",
        "type": "test.event",
        "payload": "not a dict",
        "actor": "tester",
        "context": "also not a dict",
    })
    events = event_bus.query(limit=10)
    ev = next(e for e in events if e["id"] == "norm-1")
    assert ev["payload"] == {}
    assert ev["context"] == {}


def test_emit_with_custom_actor_and_context(event_bus):
    """emit accepts actor and context kwargs."""
    event_id = event_bus.emit(
        "custom.event",
        {"data": 42},
        actor="deployer",
        context={"env": "prod"},
    )
    events = event_bus.query(limit=10)
    ev = next(e for e in events if e["id"] == event_id)
    assert ev["actor"] == "deployer"
    assert ev["context"] == {"env": "prod"}


def test_query_returns_empty_list_when_no_events(event_bus):
    """query returns [] for fresh bus."""
    assert event_bus.query(limit=50) == []


def test_emit_returns_unique_ids(event_bus):
    """Each emit call returns a unique event ID."""
    ids = {event_bus.emit("uniq.event", {"i": i}) for i in range(5)}
    assert len(ids) == 5


def test_emit_with_none_payload_stores_none(event_bus):
    """emit with None payload stores None (no coercion)."""
    event_bus.emit("null.event", None)
    events = event_bus.query(limit=10)
    ev = next(e for e in events if e["type"] == "null.event")
    assert ev["payload"] is None
