from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from cerebellum.episode_store import EpisodeStore


def _event(event_id: str, event_type: str, timestamp: datetime, **payload):
    return {
        "id": event_id,
        "timestamp": timestamp.isoformat(),
        "type": event_type,
        "payload": payload,
        "actor": payload.pop("actor", "system"),
    }


@pytest.fixture
def episode_store(tmp_path, monkeypatch):
    # Unset API key so LLM path is always skipped
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "hippocampus": {
                    "graph_path": str(tmp_path / "graph"),
                    "openrouter_url": "https://openrouter.ai/api/v1/chat/completions",
                    "openrouter_model": "openai/gpt-4o",
                }
            }
        )
    )
    store = EpisodeStore(str(config_path))
    try:
        yield store
    finally:
        store.close()


def test_add_event_and_query_events_by_type(episode_store):
    timestamp = datetime.now(UTC)
    episode_store.add_event(
        _event(
            "event-1",
            "build.finished",
            timestamp,
            project="alpha",
            user="josh",
            path="/srv/app/main.py",
            latency_ms=12,
        )
    )

    result = episode_store.query("events about build.finished")

    assert result["ok"] is True
    assert result["mode"] == "heuristic"
    assert result["query"] == "events_by_type"
    assert len(result["rows"]) == 1
    assert result["rows"][0]["id"] == "event-1"
    assert result["rows"][0]["type"] == "build.finished"


def test_create_episode_clusters_events_and_entities(episode_store):
    start = datetime.now(UTC)
    events = [
        _event(
            "episode-event-1",
            "deploy.start",
            start,
            actor="ops",
            project="alpha",
            service="api",
            user="josh",
            path="/srv/api/deploy.py",
        ),
        _event(
            "episode-event-2",
            "deploy.finish",
            start + timedelta(minutes=5),
            actor="ops",
            project="alpha",
            service="api",
            user="josh",
            path="/srv/api/deploy.py",
        ),
    ]

    episode_id = episode_store.create_episode(events)
    episodes = episode_store.get_recent_episodes()

    assert episode_id.startswith("episode:")
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["id"] == episode_id
    assert episode["event_count"] == 2
    assert episode["title"] == "deploy.start, deploy.finish episode"
    entity_types = {entity["type"] for entity in episode["entities"]}
    assert {"file", "person", "project", "service"}.issubset(entity_types)


def test_mine_successor_edges_and_query_patterns(episode_store):
    base = datetime.now(UTC) - timedelta(hours=12)
    for index in range(6):
        offset = timedelta(hours=index * 2)
        episode_store.add_event(
            _event(f"start-{index}", "deploy.start", base + offset, service="api")
        )
        episode_store.add_event(
            _event(
                f"finish-{index}",
                "deploy.finish",
                base + offset + timedelta(minutes=1),
                service="api",
            )
        )

    edges = episode_store.mine_successor_edges(window_hours=24)
    result = episode_store.query("show successor patterns")

    assert len(edges) == 1
    assert edges[0]["source_type"] == "deploy.start"
    assert edges[0]["target_type"] == "deploy.finish"
    assert edges[0]["support"] == 6
    assert edges[0]["confidence"] == 1.0
    assert result["ok"] is True
    assert result["mode"] == "heuristic"
    assert result["query"] == "successor_edges"
    assert result["rows"][0]["id"] == edges[0]["id"]
    assert result["rows"][0]["source_type"] == "deploy.start"
    assert result["rows"][0]["target_type"] == "deploy.finish"


def test_extract_entities_from_payload(episode_store):
    payload = {
        "user": "josh",
        "service": "api",
        "path": "/srv/app/main.py",
        "project": "alpha",
        "region": "us-east-1",
    }
    entities = EpisodeStore.extract_entities(payload)
    entity_map = {e["name"]: e["type"] for e in entities}

    assert entity_map["josh"] == "person"
    assert entity_map["api"] == "service"
    assert entity_map["/srv/app/main.py"] == "file"
    assert entity_map["alpha"] == "project"
    assert entity_map["us-east-1"] == "region"


def test_normalize_event_with_missing_fields(episode_store):
    event = {"type": "test.event"}
    normalized = episode_store._normalize_event(event)

    assert normalized["type"] == "test.event"
    assert normalized["actor"] == "system"
    assert normalized["id"].startswith("event:")
    assert "timestamp" in normalized


def test_query_fallback_on_llm_failure(episode_store):
    episode_store.add_event(
        _event("fallback-1", "test.event", datetime.now(UTC), data="hello")
    )
    result = episode_store.query("tell me something")

    assert result["ok"] is True
    assert result["mode"] == "heuristic"


def test_is_safe_read_query_rejects_writes(episode_store):
    assert episode_store._is_safe_read_query("MATCH (n:Event) RETURN n") is True
    assert episode_store._is_safe_read_query("CREATE (n:Event)") is False
    assert episode_store._is_safe_read_query("MATCH (n) DELETE n") is False
    assert episode_store._is_safe_read_query("") is False
    assert episode_store._is_safe_read_query("MATCH (n); DROP") is False


def test_heuristic_query_recent_episodes(episode_store):
    result = episode_store.query("show recent episodes")
    assert result["ok"] is True
    assert result["mode"] == "heuristic"
    assert result["query"] == "recent_episodes"
    assert isinstance(result["rows"], list)


def test_heuristic_query_recent_events(episode_store):
    episode_store.add_event(
        _event("recent-1", "ci.build", datetime.now(UTC), step="compile")
    )
    result = episode_store.query("what happened recently")
    assert result["ok"] is True
    assert result["mode"] == "heuristic"
    assert result["query"] == "recent_events"
    assert len(result["rows"]) >= 1


def test_mine_successor_edges_empty_on_few_events(episode_store):
    episode_store.add_event(
        _event("lone-1", "lone.event", datetime.now(UTC))
    )
    edges = episode_store.mine_successor_edges(window_hours=24)
    assert edges == []


def test_get_recent_episodes_empty(episode_store):
    episodes = episode_store.get_recent_episodes()
    assert episodes == []


def test_query_successor_edges_empty(episode_store):
    result = episode_store.query("show successor patterns")
    assert result["ok"] is True
    assert result["mode"] == "heuristic"
    assert result["query"] == "successor_edges"
    assert result["rows"] == []
