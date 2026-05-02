"""Tests for observatory_main.py — ObservatoryService lifecycle and relay logic."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture()
def config_path(tmp_path: Path):
    """Create a minimal config for EventBus."""
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({
            "sqlite": {"events_db": str(tmp_path / "events.db")},
            "nats": {"host": "localhost", "port": 4222},
        })
    )
    return config


class TestObservatoryServiceInit:
    def test_init_state(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        assert svc.stop_requested is False
        assert svc._emitter is None
        assert svc._nats_client is None

    def test_relay_actor_constant(self):
        from cerebellum.observatory_main import ObservatoryService
        assert ObservatoryService.RELAY_ACTOR == "observatory.nats-subscriber"


class TestObservatoryServiceSignalHandlers:
    @pytest.mark.asyncio
    async def test_request_stop_sets_flag(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        svc._request_stop()
        assert svc.stop_requested is True

    @pytest.mark.asyncio
    async def test_install_signal_handlers(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        asyncio.get_running_loop()
        svc._install_signal_handlers()
        # No exception means handlers installed successfully
        assert svc.stop_requested is False


class TestObservatoryServiceEmitter:
    @pytest.mark.asyncio
    async def test_start_emitter_success(self, config_path):
        from cerebellum.observatory_main import ObservatoryService
        with patch("cerebellum.observatory_main.EventBus") as MockEmitter:
            MockEmitter.return_value = MagicMock()
            svc = ObservatoryService()
            with patch.object(svc, "__class__.__dict__['EventBus']", MockEmitter, create=True):
                pass
            # Direct test: patch at module level
            import cerebellum.observatory_main as obs_mod
            with patch.object(obs_mod, "EventBus", MagicMock()) as mock_cls:
                svc._emitter = None
                mock_cls.return_value = MagicMock()
                await svc._start_emitter()
                assert svc._emitter is not None

    @pytest.mark.asyncio
    async def test_start_emitter_failure(self, config_path):
        import cerebellum.observatory_main as obs_mod
        from cerebellum.observatory_main import ObservatoryService
        with patch.object(obs_mod, "EventBus", side_effect=RuntimeError("no config")):
            svc = ObservatoryService()
            await svc._start_emitter()
            assert svc._emitter is None


class TestObservatoryServiceNATSLoop:
    @pytest.mark.asyncio
    async def test_nats_loop_no_nats_library(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        svc.stop_requested = True  # ensure loop exits immediately
        with patch("importlib.util.find_spec", return_value=None):
            await svc._run_nats_loop()
        assert svc._nats_client is None

    @pytest.mark.asyncio
    async def test_nats_loop_connection_failure(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        svc.stop_requested = True
        import nats  # noqa: F401 - ensure module is found
        with patch("nats.connect", side_effect=RuntimeError("connection refused")):
            await svc._run_nats_loop()


class TestObservatoryServiceShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_no_clients(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        await svc._shutdown()  # should not raise

    @pytest.mark.asyncio
    async def test_shutdown_with_nats_client(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        mock_nc = AsyncMock()
        svc._nats_client = mock_nc
        await svc._shutdown()
        mock_nc.drain.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_with_emitter(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        mock_emitter = MagicMock()
        svc._emitter = mock_emitter
        await svc._shutdown()
        mock_emitter.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_nats_drain_failure(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        mock_nc = AsyncMock()
        mock_nc.drain.side_effect = RuntimeError("drain failed")
        svc._nats_client = mock_nc
        await svc._shutdown()  # should not raise

    @pytest.mark.asyncio
    async def test_shutdown_emitter_close_failure(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        mock_emitter = MagicMock()
        mock_emitter.close.side_effect = RuntimeError("close failed")
        svc._emitter = mock_emitter
        await svc._shutdown()  # should not raise


class TestRelayEvent:
    @pytest.fixture()
    def svc_with_emitter(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        mock_emitter = MagicMock()
        svc._emitter = mock_emitter
        return svc, mock_emitter

    def test_relay_event_none_emitter(self):
        from cerebellum.observatory_main import ObservatoryService
        svc = ObservatoryService()
        svc._relay_event("test.topic", '{"id": "1"}')
        # Should silently return when _emitter is None

    def test_relay_event_malformed_json(self, svc_with_emitter):
        svc, mock_emitter = svc_with_emitter
        svc._relay_event("test.topic", "not json{{{")
        mock_emitter.write_event.assert_not_called()

    def test_relay_event_non_dict_json(self, svc_with_emitter):
        svc, mock_emitter = svc_with_emitter
        svc._relay_event("test.topic", '"just a string"')
        mock_emitter.write_event.assert_not_called()

    def test_relay_event_relay_origin_dropped(self, svc_with_emitter):
        svc, mock_emitter = svc_with_emitter
        svc._relay_event("test.topic", json.dumps({"id": "1", "actor": "observatory.nats-subscriber"}))
        mock_emitter.write_event.assert_not_called()

    def test_relay_event_no_id_dropped(self, svc_with_emitter):
        svc, mock_emitter = svc_with_emitter
        svc._relay_event("test.topic", json.dumps({"actor": "someone"}))
        mock_emitter.write_event.assert_not_called()

    def test_relay_event_whitespace_id_dropped(self, svc_with_emitter):
        svc, mock_emitter = svc_with_emitter
        svc._relay_event("test.topic", json.dumps({"id": "   "}))
        mock_emitter.write_event.assert_not_called()

    def test_relay_event_success(self, svc_with_emitter):
        svc, mock_emitter = svc_with_emitter
        payload = {
            "id": "evt-123",
            "type": "test.event",
            "timestamp": "2025-01-01T00:00:00",
            "actor": "tester",
            "payload": {"key": "val"},
            "context": {"ctx": "data"},
        }
        svc._relay_event("test.topic", json.dumps(payload))
        mock_emitter.write_event.assert_called_once()
        call_args = mock_emitter.write_event.call_args[0][0]
        assert call_args["id"] == "evt-123"
        assert call_args["type"] == "test.event"
        assert call_args["actor"] == "tester"
        assert call_args["payload"] == {"key": "val"}
        assert call_args["context"] == {"ctx": "data"}

    def test_relay_event_type_from_topic_fallback(self, svc_with_emitter):
        svc, mock_emitter = svc_with_emitter
        payload = {"id": "evt-456", "actor": "tester"}
        svc._relay_event("cerebellum.events.my.custom", json.dumps(payload))
        call_args = mock_emitter.write_event.call_args[0][0]
        assert call_args["type"] == "my.custom"

    def test_relay_event_payload_normalized(self, svc_with_emitter):
        svc, mock_emitter = svc_with_emitter
        payload = {"id": "evt-789", "payload": "not a dict", "context": [1, 2]}
        svc._relay_event("test.topic", json.dumps(payload))
        call_args = mock_emitter.write_event.call_args[0][0]
        assert call_args["payload"] == {}
        assert call_args["context"] == {}

    def test_relay_event_relay_exception_handled(self, svc_with_emitter):
        svc, mock_emitter = svc_with_emitter
        mock_emitter.write_event.side_effect = RuntimeError("db error")
        svc._relay_event("test.topic", json.dumps({"id": "evt-err"}))
        # Should not raise


class TestMain:
    @pytest.mark.asyncio
    async def test_async_main_returns_zero(self):
        from cerebellum.observatory_main import ObservatoryService
        with patch("cerebellum.observatory_main.ObservatoryService") as MockSvc:
            mock_instance = MagicMock(spec=ObservatoryService)
            mock_instance.run = AsyncMock()
            MockSvc.return_value = mock_instance
            import cerebellum.observatory_main as obs_mod
            result = await obs_mod._async_main()
            assert result == 0
            mock_instance.run.assert_called_once()

    def test_main_keyboard_interrupt(self):
        import cerebellum.observatory_main as obs_mod
        with patch.object(obs_mod, "_async_main", new_callable=AsyncMock) as mock_async_main:
            mock_async_main.side_effect = KeyboardInterrupt()
            result = obs_mod.main()
            assert result == 0

    def test_main_exception_returns_one(self):
        import cerebellum.observatory_main as obs_mod
        with patch.object(obs_mod, "_async_main", new_callable=AsyncMock) as mock_async_main:
            mock_async_main.side_effect = RuntimeError("crash")
            result = obs_mod.main()
            assert result == 1
