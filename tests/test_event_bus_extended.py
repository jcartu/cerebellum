"""Extended tests for event_bus.py — deeper coverage of missing branches."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def config_path(tmp_path: Path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({
            "sqlite": {"events_db": str(tmp_path / "events.db")},
            "nats": {"host": "localhost", "port": 4222},
        })
    )
    return config


@pytest.fixture()
def emitter(config_path, monkeypatch):
    """Create an EventBus that works without NATS."""
    monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
    from cerebellum.event_bus import EventBus
    eb = EventBus(config_path)
    yield eb
    eb.close()


class TestWriteEvent:
    def test_write_event_success(self, emitter):
        emitter.write_event({
            "id": "we-1",
            "timestamp": "2025-01-01T00:00:00",
            "type": "test.event",
            "payload": {"key": "val"},
            "actor": "tester",
            "context": {},
        })
        results = emitter.query(limit=10)
        assert len(results) == 1
        assert results[0]["id"] == "we-1"

    def test_write_event_missing_keys_raises(self, emitter):
        with pytest.raises(ValueError, match="missing required keys"):
            emitter.write_event({"id": "we-2"})

    def test_write_event_missing_specific_keys(self, emitter):
        with pytest.raises(ValueError) as exc_info:
            emitter.write_event({"id": "we-3", "timestamp": "2025-01-01"})
        assert "actor" in str(exc_info.value)
        assert "context" in str(exc_info.value)
        assert "payload" in str(exc_info.value)
        assert "type" in str(exc_info.value)

    def test_write_event_normalizes_non_dict_payload(self, emitter):
        emitter.write_event({
            "id": "we-4",
            "timestamp": "2025-01-01T00:00:00",
            "type": "test",
            "payload": "not a dict",
            "actor": "tester",
            "context": [1, 2],
        })
        results = emitter.query(limit=10)
        assert results[0]["payload"] == {}
        assert results[0]["context"] == {}

    def test_write_event_with_or_ignore_duplicate(self, emitter):
        emitter.write_event({
            "id": "we-dup",
            "timestamp": "2025-01-01T00:00:00",
            "type": "test",
            "payload": {},
            "actor": "tester",
            "context": {},
        })
        emitter.write_event({
            "id": "we-dup",
            "timestamp": "2025-01-02T00:00:00",
            "type": "test2",
            "payload": {"v": 2},
            "actor": "tester",
            "context": {},
        })
        results = emitter.query(limit=10)
        assert len(results) == 1
        assert results[0]["type"] == "test"

    def test_write_event_defaults_timestamp(self, emitter):
        emitter.write_event({
            "id": "we-ts",
            "timestamp": "",
            "type": "",
            "payload": {},
            "actor": "",
            "context": {},
        })
        results = emitter.query(limit=10)
        assert results[0]["type"] == "unknown"
        assert results[0]["actor"] == "system"


class TestQuery:
    def test_query_by_type(self, emitter):
        emitter.write_event({"id": "q-1", "timestamp": "2025-01-01T00:00:00", "type": "type_a", "payload": {}, "actor": "t", "context": {}})
        emitter.write_event({"id": "q-2", "timestamp": "2025-01-01T00:00:01", "type": "type_b", "payload": {}, "actor": "t", "context": {}})
        results = emitter.query(types=["type_a"], limit=10)
        assert len(results) == 1
        assert results[0]["type"] == "type_a"

    def test_query_by_since(self, emitter):
        emitter.write_event({"id": "q-3", "timestamp": "2025-01-01T00:00:00", "type": "t", "payload": {}, "actor": "t", "context": {}})
        emitter.write_event({"id": "q-4", "timestamp": "2025-01-02T00:00:00", "type": "t", "payload": {}, "actor": "t", "context": {}})
        # Since uses string comparison: stored timestamps have no timezone suffix
        # so we use a datetime that produces a string before both stored timestamps
        since = datetime(2024, 12, 31, tzinfo=UTC)
        results = emitter.query(since=since, limit=10)
        assert len(results) == 2
        # Results are ordered DESC by timestamp
        assert results[0]["id"] == "q-4"

    def test_query_combined_filters(self, emitter):
        emitter.write_event({"id": "q-5", "timestamp": "2025-01-01T00:00:00", "type": "t", "payload": {}, "actor": "t", "context": {}})
        emitter.write_event({"id": "q-6", "timestamp": "2025-01-02T00:00:00", "type": "t2", "payload": {}, "actor": "t", "context": {}})
        since = datetime(2025, 1, 2, tzinfo=UTC)
        results = emitter.query(types=["t"], since=since, limit=10)
        assert len(results) == 0

    def test_query_limit(self, emitter):
        for i in range(5):
            emitter.write_event({"id": f"q-{i}", "timestamp": f"2025-01-0{i}T00:00:00", "type": "t", "payload": {}, "actor": "t", "context": {}})
        results = emitter.query(limit=3)
        assert len(results) == 3

    def test_query_empty(self, emitter):
        results = emitter.query(limit=10)
        assert results == []

    def test_query_exception_returns_empty(self, emitter):
        with patch.object(emitter, "_sqlite", wraps=emitter._sqlite) as mock_db:
            mock_db.execute.side_effect = sqlite3.OperationalError("db locked")
            results = emitter.query(limit=10)
            assert results == []


class TestSubscribe:
    def test_subscribe_skips_when_nats_unavailable(self, emitter):
        def cb(event):
            pass
        emitter.subscribe(cb)
        # Should not raise, just log warning


class TestLoadConfig:
    def test_load_config_file_not_found(self, emitter, tmp_path):
        with pytest.raises(FileNotFoundError):
            emitter._load_config(tmp_path / "nonexistent.json")

    def test_load_config_invalid_json(self, emitter, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{invalid json}")
        with pytest.raises(Exception):
            emitter._load_config(bad)


class TestConfigureSQLite:
    def test_configure_sqlite_creates_tables(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            with eb._db_lock:
                rows = eb._sqlite.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'").fetchall()
            assert len(rows) == 1
            rows = eb._sqlite.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
            index_names = [r[0] for r in rows]
            assert "idx_events_timestamp" in index_names
            assert "idx_events_type_timestamp" in index_names
        finally:
            eb.close()


class TestCheckpointWorker:
    def test_checkpoint_worker_runs(self, emitter):
        # The worker is a daemon thread; just verify it starts and doesn't crash
        import time
        time.sleep(0.1)
        assert emitter._checkpoint_thread is not None
        assert emitter._checkpoint_thread.is_alive()


class TestEmitWithoutNATS:
    def test_emit_stores_in_sqlite_only(self, emitter):
        event_id = emitter.emit("test.nats", {"data": "val"}, actor="test")
        assert event_id
        results = emitter.query(limit=10)
        assert len(results) == 1
        assert results[0]["type"] == "test.nats"
