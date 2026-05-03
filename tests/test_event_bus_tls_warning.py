from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cerebellum.event_bus import EventBus


def test_nats_tls_warning_when_disabled(tmp_path, caplog, monkeypatch):
    """A WARNING is logged when NATS connects without TLS."""
    monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "test-token")

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "sqlite": {"events_db": str(tmp_path / "events.sqlite3")},
                "nats": {"host": "127.0.0.1", "port": 4222, "tls": False},
            }
        )
    )

    mock_js = AsyncMock()
    mock_js.stream_info = AsyncMock(side_effect=Exception("no stream"))
    mock_js.add_stream = AsyncMock()

    mock_nc = MagicMock()
    mock_nc.connect = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats_cls = MagicMock(return_value=mock_nc)

    with patch("cerebellum.event_bus.NATS", mock_nats_cls), \
         caplog.at_level("WARNING"):
        bus = EventBus(config_path)
        try:
            assert bus._nats_ready is True
            assert "WITHOUT TLS" in caplog.text
        finally:
            bus.close()


def test_nats_no_tls_warning_when_enabled(tmp_path, caplog, monkeypatch):
    """No TLS warning when TLS is enabled."""
    monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "test-token")

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "sqlite": {"events_db": str(tmp_path / "events.sqlite3")},
                "nats": {"host": "127.0.0.1", "port": 4222, "tls": True},
            }
        )
    )

    mock_js = AsyncMock()
    mock_js.stream_info = AsyncMock(side_effect=Exception("no stream"))
    mock_js.add_stream = AsyncMock()

    mock_nc = MagicMock()
    mock_nc.connect = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)

    mock_nats_cls = MagicMock(return_value=mock_nc)

    with patch("cerebellum.event_bus.NATS", mock_nats_cls), \
         caplog.at_level("WARNING"):
        bus = EventBus(config_path)
        try:
            assert bus._nats_ready is True
            assert "WITHOUT TLS" not in caplog.text
        finally:
            bus.close()
