"""More coverage tests for event_bus publish/subscribe paths."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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


class TestEventBusPublishSubscribe:
    @pytest.mark.asyncio
    async def test_publish_event_no_js(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            eb._js = None
            await eb._publish_event({"type": "test", "id": "1"})
        finally:
            eb.close()

    @pytest.mark.asyncio
    async def test_publish_event_success(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            mock_js = AsyncMock()
            eb._js = mock_js
            await eb._publish_event({"type": "test.event", "id": "1"})
            mock_js.publish.assert_called_once()
            call_args = mock_js.publish.call_args
            assert call_args[0][0] == "cerebellum.events.test.event"
        finally:
            eb.close()

    @pytest.mark.asyncio
    async def test_publish_event_failure(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            mock_js = AsyncMock()
            mock_js.publish.side_effect = Exception("publish failed")
            eb._js = mock_js
            with pytest.raises(Exception, match="publish failed"):
                await eb._publish_event({"type": "test.event", "id": "1"})
        finally:
            eb.close()

    @pytest.mark.asyncio
    async def test_subscribe_no_nc(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            eb._nc = None
            await eb._subscribe(lambda e: None)
        finally:
            eb.close()

    @pytest.mark.asyncio
    async def test_subscribe_success(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            mock_nc = AsyncMock()
            eb._nc = mock_nc
            await eb._subscribe(lambda e: None)
            mock_nc.subscribe.assert_called_once()
            assert mock_nc.subscribe.call_args[0][0] == "cerebellum.events.>"
        finally:
            eb.close()

    def test_wait_for_inflight_publishes_empty(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            result = eb._wait_for_inflight_publishes(1.0)
            assert result == 0
        finally:
            eb.close()

    def test_row_to_event(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            class MockRow:
                def __init__(self):
                    self._data = {
                        "id": "evt-1",
                        "timestamp": "2025-01-01T00:00:00",
                        "type": "test.event",
                        "payload": json.dumps({"key": "val"}),
                        "actor": "tester",
                        "context": json.dumps({"ctx": "data"}),
                    }
                def __getitem__(self, key):
                    return self._data[key]
            row = MockRow()
            event = eb._row_to_event(row)
            assert event["id"] == "evt-1"
            assert event["type"] == "test.event"
            assert event["payload"] == {"key": "val"}
            assert event["context"] == {"ctx": "data"}
        finally:
            eb.close()
