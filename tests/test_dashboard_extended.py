"""Tests for dashboard.py — deeper coverage of missing branches (rate limiters, schema migration, etc.)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def config_path(tmp_path: Path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({
            "sqlite": {"events_db": str(tmp_path / "events.db")},
            "nats": {"host": "localhost", "port": 4222},
            "dashboard": {"port": 18790},
        })
    )
    return config


class TestAllowTelegramWebhookIP:
    def test_allow_first_request(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        dash._telegram_webhook_rate_windows.clear()
        result = dash._allow_telegram_webhook_ip("127.0.0.1")
        assert result is True

    def test_rate_limit_after_60_requests(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        dash._telegram_webhook_rate_windows.clear()
        now_window = int(time.time() // 60)
        # Pre-fill with 60 requests
        dash._telegram_webhook_rate_windows["10.0.0.1"] = (now_window, 60)
        result = dash._allow_telegram_webhook_ip("10.0.0.1")
        assert result is False

    def test_stale_windows_cleaned(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        dash._telegram_webhook_rate_windows.clear()
        old_window = int(time.time() // 60) - 10
        dash._telegram_webhook_rate_windows["stale.ip"] = (old_window, 50)
        # New request from different IP should clean stale entries
        dash._allow_telegram_webhook_ip("127.0.0.1")
        assert "stale.ip" not in dash._telegram_webhook_rate_windows


class TestAllowAuthenticatedRequest:
    def test_allow_first_request(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        dash._auth_rate_windows.clear()
        result = dash._allow_authenticated_request("127.0.0.1")
        assert result is True

    def test_rate_limit_after_60_requests(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        dash._auth_rate_windows.clear()
        now_window = int(time.time() // 60)
        dash._auth_rate_windows["10.0.0.2"] = (now_window, 60)
        result = dash._allow_authenticated_request("10.0.0.2")
        assert result is False

    def test_stale_windows_cleaned(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        dash._auth_rate_windows.clear()
        old_window = int(time.time() // 60) - 10
        dash._auth_rate_windows["stale.ip"] = (old_window, 50)
        dash._allow_authenticated_request("127.0.0.1")
        assert "stale.ip" not in dash._auth_rate_windows


class TestRenderEvents:
    def test_render_events_empty(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        result = dash._render_events([])
        assert "No events yet" in result

    def test_render_events_with_data(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        events = [
            {
                "id": "evt-1",
                "type": "test.event",
                "timestamp": "2025-01-01T00:00:00",
                "actor": "tester",
                "payload": {"key": "val"},
                "context": {"ctx": "data"},
            }
        ]
        result = dash._render_events(events)
        assert "test.event" in result
        assert "tester" in result
        assert "evt-1" in result
        assert "key" in result

    def test_render_events_escapes_html(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        events = [
            {
                "id": "evt-<script>",
                "type": "test<script>alert(1)</script>",
                "timestamp": "2025-01-01T00:00:00",
                "actor": "<b>evil</b>",
                "payload": {"html": "<div>"},
                "context": {},
            }
        ]
        result = dash._render_events(events)
        assert "<script>" not in result
        assert "&lt;script&gt;" in result


class TestRequestSourceIP:
    def test_ip_from_x_forwarded_for(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        from fastapi import Request
        from starlette.datastructures import Address

        import cerebellum.ui.dashboard as dash
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}
        mock_request.client = Address("127.0.0.1", 8080)
        result = dash._request_source_ip(mock_request)
        assert result == "1.2.3.4"

    def test_ip_from_client(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        from fastapi import Request
        from starlette.datastructures import Address

        import cerebellum.ui.dashboard as dash
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {}
        mock_request.client = Address("192.168.1.1", 12345)
        result = dash._request_source_ip(mock_request)
        assert result == "192.168.1.1"

    def test_ip_unknown(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        from fastapi import Request

        import cerebellum.ui.dashboard as dash
        mock_request = MagicMock(spec=Request)
        mock_request.headers = {}
        mock_request.client = None
        result = dash._request_source_ip(mock_request)
        assert result == "unknown"


class TestParseSince:
    def test_parse_since_none(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        assert dash._parse_since(None) is None

    def test_parse_since_valid(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        result = dash._parse_since("2025-01-01T00:00:00+00:00")
        assert result is not None
        assert result.year == 2025

    def test_parse_since_invalid(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        result = dash._parse_since("not-a-date")
        assert result is None


class TestEnsureTelegramSeenUpdatesSchema:
    def test_creates_table_if_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        db_path = tmp_path / "test.db"
        db = sqlite3.connect(str(db_path))
        dash._ensure_telegram_seen_updates_schema(db)
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [r[0] for r in tables]
        assert "telegram_seen_updates" in table_names
        db.close()

    def test_rebuilds_if_update_id_is_integer(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        import cerebellum.ui.dashboard as dash
        db_path = tmp_path / "test.db"
        db = sqlite3.connect(str(db_path))
        # Create old schema with INTEGER update_id
        db.execute("CREATE TABLE telegram_seen_updates (update_id INTEGER PRIMARY KEY, seen_at INTEGER NOT NULL)")
        db.commit()
        # Insert a row
        db.execute("INSERT INTO telegram_seen_updates VALUES (12345, 1000)")
        db.commit()
        # Run migration
        dash._ensure_telegram_seen_updates_schema(db)
        # Check new schema
        info = db.execute("PRAGMA table_info(telegram_seen_updates)").fetchall()
        update_id_row = next(r for r in info if r[1] == "update_id")
        assert update_id_row[2].upper() == "TEXT"
        # Check data migrated
        rows = db.execute("SELECT update_id FROM telegram_seen_updates").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "12345"
        # Legacy table should be dropped
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [r[0] for r in tables]
        assert "telegram_seen_updates_legacy" not in table_names
        db.close()


class TestStatsPayload:
    def test_stats_payload(self, config_path, monkeypatch):
        monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        monkeypatch.setattr("cerebellum.ui.dashboard.CONFIG_PATH", config_path)
        import cerebellum.ui.dashboard as dash
        # Reset emitter singleton
        dash._emitter = None
        payload = dash._stats_payload()
        assert "window" in payload
        assert "counts" in payload
        assert "total" in payload
        assert payload["window"] == "24h"
        dash._emitter = None
