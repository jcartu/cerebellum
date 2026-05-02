"""Additional coverage tests for event_bus NATS paths and policy_arbiter."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestEventBusNATSMocking:
    @pytest.mark.asyncio
    async def test_connect_to_nats_async_success(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "test-token")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            mock_nats = MagicMock()
            mock_nats.connect = AsyncMock()
            mock_js = AsyncMock()
            mock_nats.jetstream = MagicMock(return_value=mock_js)
            mock_js.stream_info = AsyncMock()
            with patch("cerebellum.event_bus.NATS", return_value=mock_nats):
                eb._nats_ready = False
                eb._nc = None
                eb._js = None
                await eb._connect_to_nats_async()
                assert eb._nats_ready is True
        finally:
            eb.close()

    @pytest.mark.asyncio
    async def test_connect_to_nats_async_create_stream(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "test-token")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            mock_nats = MagicMock()
            mock_nats.connect = AsyncMock()
            mock_js = AsyncMock()
            mock_nats.jetstream = MagicMock(return_value=mock_js)
            mock_js.stream_info = AsyncMock(side_effect=Exception("stream not found"))
            mock_js.add_stream = AsyncMock()
            with patch("cerebellum.event_bus.NATS", return_value=mock_nats):
                eb._nats_ready = False
                eb._nc = None
                eb._js = None
                await eb._connect_to_nats_async()
                assert eb._nats_ready is True
                mock_js.add_stream.assert_called_once()
        finally:
            eb.close()

    @pytest.mark.asyncio
    async def test_connect_to_nats_async_all_retries_fail(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "test-token")
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            mock_nats = MagicMock()
            mock_nats.connect = AsyncMock(side_effect=ConnectionError("refused"))
            with patch("cerebellum.event_bus.NATS", return_value=mock_nats):
                eb._nats_ready = False
                eb._nc = None
                eb._js = None
                with pytest.raises(RuntimeError, match="NATS connection failed"):
                    await eb._connect_to_nats_async()
                assert eb._nats_ready is False
        finally:
            eb.close()

    @pytest.mark.asyncio
    async def test_connect_to_nats_async_no_token(self, config_path, monkeypatch):
        monkeypatch.delenv("CEREBELLUM_NATS_TOKEN", raising=False)
        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            with pytest.raises(RuntimeError, match="CEREBELLUM_NATS_TOKEN is not configured"):
                await eb._connect_to_nats_async()
        finally:
            eb.close()


class TestPolicyArbiterAdditional:
    def test_validate_file_path_allowed_root(self, monkeypatch, config_path, tmp_path):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        from cerebellum.policy_arbiter import PolicyArbiter
        eb = EventBus(config_path)
        try:
            test_file = tmp_path / "test.txt"
            test_file.write_text("test")
            policy = tmp_path / "policy.yaml"
            policy.write_text(f"allowed_roots:\n  - {tmp_path}\n")
            arbiter = PolicyArbiter(str(policy), emitter=eb)
            arbiter._validate_file_path(test_file)
        finally:
            eb.close()

    def test_validate_file_path_symlink_outside_root(self, monkeypatch, config_path, tmp_path):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        from cerebellum.policy_arbiter import PolicyArbiter
        eb = EventBus(config_path)
        try:
            policy = tmp_path / "policy.yaml"
            policy.write_text("allowed_roots:\n  - /tmp\n")
            arbiter = PolicyArbiter(str(policy), emitter=eb)
            with pytest.raises(ValueError):
                arbiter._validate_file_path(Path("/tmp/nonexistent/../etc/passwd"))
        finally:
            eb.close()

    def test_kill_switch_toggle(self, monkeypatch, config_path):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        from cerebellum.event_bus import EventBus
        from cerebellum.policy_arbiter import PolicyArbiter
        eb = EventBus(config_path)
        try:
            policy = config_path.parent / "policy.yaml"
            policy.write_text("allowed_roots:\n  - /tmp\n")
            arbiter = PolicyArbiter(str(policy), emitter=eb)
            result = arbiter.toggle_kill_switch(enabled=True)
            assert result["kill_switch"] is True
            result = arbiter.toggle_kill_switch(enabled=False)
            assert result["kill_switch"] is False
        finally:
            eb.close()