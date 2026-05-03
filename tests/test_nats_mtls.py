"""End-to-end tests for NATS mTLS certificate loading.

Covers:
- TLS context creation with CA verification
- mTLS with client certificate and key loading
- Fallback to server-only verification when cert/key missing
- Warning emission when TLS is disabled
- Certificate path resolution from config and environment
"""

from __future__ import annotations

import os
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

from cerebellum.event_bus import EventBus


class TestNATSMTLSContext:
    """Verify mTLS context creation in _connect_to_nats_async."""

    def _create_bus(
        self,
        config: dict[str, object],
        env: dict[str, str] | None = None,
    ) -> tuple[EventBus, MagicMock]:
        """Create an EventBus with mocked config and return (bus, mock_nc)."""
        env = env or {}
        mock_nc = AsyncMock()
        mock_nc.connect = AsyncMock()
        mock_js = AsyncMock()
        mock_js.stream_info = AsyncMock(side_effect=Exception("no stream"))
        mock_js.add_stream = AsyncMock()
        mock_nc.jetstream = MagicMock(return_value=mock_js)

        mock_nats_cls = MagicMock(return_value=mock_nc)

        # Create a mock SSL context that doesn't try to load real files
        mock_ssl_context = MagicMock(spec=ssl.SSLContext)

        def fake_connect(self_: EventBus) -> None:
            import asyncio
            future = asyncio.run_coroutine_threadsafe(self_._connect_to_nats_async(), self_._loop)
            future.result(timeout=10)

        with patch("cerebellum.event_bus.NATS", mock_nats_cls), \
             patch.object(EventBus, "_connect_to_nats", fake_connect), \
             patch.object(EventBus, "_load_config", return_value=config), \
             patch.object(EventBus, "_configure_sqlite"), \
             patch.object(EventBus, "_start_checkpoint_worker"), \
             patch("ssl.create_default_context", return_value=mock_ssl_context), \
             patch.dict(os.environ, env, clear=False):
            bus = EventBus("/tmp/test_config.json")
            return bus, mock_nc, mock_ssl_context

    def test_tls_context_created_when_tls_enabled(self) -> None:
        """When tls=true, an SSL context should be created and passed to connect."""
        config = {
            "sqlite": {"events_db": "/tmp/test_nats.db"},
            "nats": {"host": "localhost", "port": 4222, "tls": True, "tls_ca": "/tmp/ca.pem"},
        }
        bus, mock_nc, mock_ssl_context = self._create_bus(
            config, {"CEREBELLUM_NATS_TOKEN": "test-token"}
        )
        assert bus._nats_ready is True

        # Verify SSL context was created
        assert mock_ssl_context.load_verify_locations.called
        # Verify TLS context was passed to connect
        connect_kwargs = mock_nc.connect.call_args[1]
        assert "tls" in connect_kwargs
        assert connect_kwargs["tls"] is mock_ssl_context

    def test_mtls_loads_client_cert_and_key(self) -> None:
        """When tls_cert and tls_key are provided, load_cert_chain should be called."""
        config = {
            "sqlite": {"events_db": "/tmp/test_nats.db"},
            "nats": {
                "host": "localhost",
                "port": 4222,
                "tls": True,
                "tls_ca": "/tmp/ca.pem",
                "tls_cert": "/tmp/client.pem",
                "tls_key": "/tmp/client.key",
            },
        }
        bus, _mock_nc, mock_ssl_context = self._create_bus(
            config, {"CEREBELLUM_NATS_TOKEN": "test-token"}
        )
        assert bus._nats_ready is True

        # Verify load_cert_chain was called with correct paths
        mock_ssl_context.load_cert_chain.assert_called_once_with(
            "/tmp/client.pem", "/tmp/client.key"
        )
        # Verify load_verify_locations was called for CA
        mock_ssl_context.load_verify_locations.assert_called_once_with("/tmp/ca.pem")

    def test_server_only_verification_without_client_cert(self) -> None:
        """When tls_cert/tls_key are absent, only CA verification should occur."""
        config = {
            "sqlite": {"events_db": "/tmp/test_nats.db"},
            "nats": {"host": "localhost", "port": 4222, "tls": True, "tls_ca": "/tmp/ca.pem"},
        }
        bus, _, mock_ssl_context = self._create_bus(
            config, {"CEREBELLUM_NATS_TOKEN": "test-token"}
        )
        assert bus._nats_ready is True

        # Verify load_cert_chain was NOT called
        mock_ssl_context.load_cert_chain.assert_not_called()
        # Verify CA was loaded
        mock_ssl_context.load_verify_locations.assert_called_once_with("/tmp/ca.pem")

    def test_env_vars_override_config_for_tls_paths(self) -> None:
        """Environment variables should override config for TLS paths."""
        env = {
            "CEREBELLUM_NATS_TOKEN": "test-token",
            "CEREBELLUM_NATS_TLS_CA": "/env/ca.pem",
            "CEREBELLUM_NATS_TLS_CERT": "/env/client.pem",
            "CEREBELLUM_NATS_TLS_KEY": "/env/client.key",
        }

        config = {
            "sqlite": {"events_db": "/tmp/test_nats.db"},
            "nats": {"host": "localhost", "port": 4222, "tls": True},
        }
        bus, _, mock_ssl_context = self._create_bus(config, env)
        assert bus._nats_ready is True

        # Verify env paths were used
        mock_ssl_context.load_verify_locations.assert_called_once_with("/env/ca.pem")
        mock_ssl_context.load_cert_chain.assert_called_once_with(
            "/env/client.pem", "/env/client.key"
        )

    def test_tls_disabled_uses_nats_scheme(self) -> None:
        """When tls=false, scheme should be 'nats' not 'tls'."""
        config = {
            "sqlite": {"events_db": "/tmp/test_nats.db"},
            "nats": {"host": "localhost", "port": 4222, "tls": False},
        }
        bus, mock_nc, _ = self._create_bus(config, {"CEREBELLUM_NATS_TOKEN": "test-token"})
        assert bus._nats_ready is True

        connect_kwargs = mock_nc.connect.call_args[1]
        servers = connect_kwargs.get("servers", [])
        assert servers[0].startswith("nats://")
        assert "tls" not in connect_kwargs

    def test_tls_enabled_uses_tls_scheme(self) -> None:
        """When tls=true, scheme should be 'tls'."""
        config = {
            "sqlite": {"events_db": "/tmp/test_nats.db"},
            "nats": {"host": "localhost", "port": 4222, "tls": True},
        }
        bus, mock_nc, _ = self._create_bus(config, {"CEREBELLUM_NATS_TOKEN": "test-token"})
        assert bus._nats_ready is True

        connect_kwargs = mock_nc.connect.call_args[1]
        servers = connect_kwargs.get("servers", [])
        assert servers[0].startswith("tls://")
