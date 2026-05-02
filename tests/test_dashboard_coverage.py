"""Dashboard coverage tests — uses monkeypatch.setattr to override module constants."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def dash():
    """Import dashboard module for patching."""
    import cerebellum.ui.dashboard as dash

    return dash


@pytest.fixture
def client(dash, monkeypatch, tmp_path):
    """Create TestClient with patched module constants and isolated DB."""
    # Use a temp DB so update_id dedup doesn't leak between test runs
    tmp_db = tmp_path / "test_dashboard.db"
    monkeypatch.setattr(dash, "_dashboard_db_path", lambda: tmp_db)
    monkeypatch.setattr(dash, "_dashboard_db", None)
    monkeypatch.setattr(dash, "TELEGRAM_BOT_TOKEN", "123456:ABC-TEST-BOT-TOKEN", raising=False)
    monkeypatch.setattr(dash, "TELEGRAM_WEBHOOK_SECRET", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", raising=False)
    monkeypatch.setattr(dash, "TELEGRAM_ALLOWED_USER_IDS", {"12345678"}, raising=False)
    return TestClient(dash.app)


@pytest.fixture(autouse=True)
def _reset_rate_windows(dash):
    """Reset rate limit windows between tests."""
    with dash._auth_rate_lock:
        dash._auth_rate_windows.clear()
    with dash._telegram_webhook_rate_lock:
        dash._telegram_webhook_rate_windows.clear()
    yield
    with dash._auth_rate_lock:
        dash._auth_rate_windows.clear()
    with dash._telegram_webhook_rate_lock:
        dash._telegram_webhook_rate_windows.clear()
    # Close dashboard DB to prevent unclosed connection warnings
    conn = dash._dashboard_db
    if conn is not None:
        conn.close()
    dash._dashboard_db = None


class TestGetEmitter:
    def test_get_emitter_returns_event_bus(self, dash, monkeypatch):
        mock_bus = MagicMock()
        monkeypatch.setattr(dash, "EventBus", MagicMock(return_value=mock_bus))
        original = dash._emitter
        dash._emitter = None
        try:
            assert dash.get_emitter() is mock_bus
        finally:
            dash._emitter = original

    def test_get_emitter_raises_on_failure(self, dash, monkeypatch):
        monkeypatch.setattr(dash, "EventBus", MagicMock(side_effect=RuntimeError("no config")))
        original = dash._emitter
        dash._emitter = None
        try:
            with pytest.raises(RuntimeError, match="emitter unavailable"):
                dash.get_emitter()
        finally:
            dash._emitter = original


class TestGetArbiter:
    def test_get_arbiter_returns_none_no_policy(self, dash, monkeypatch):
        original = dash._arbiter
        dash._arbiter = None
        try:
            with patch.object(dash.Path, "exists", return_value=False):
                assert dash.get_arbiter() is None
        finally:
            dash._arbiter = original

    def test_get_arbiter_returns_none_on_error(self, dash, monkeypatch):
        original = dash._arbiter
        dash._arbiter = None
        try:
            with patch.object(dash.Path, "exists", return_value=True):
                monkeypatch.setattr(dash, "PolicyArbiter", MagicMock(side_effect=RuntimeError("bad")))
                assert dash.get_arbiter() is None
        finally:
            dash._arbiter = original


class TestAuthMiddleware:
    def test_healthz_no_auth(self, client):
        assert client.get("/healthz").status_code == 200

    def test_unauthorized_wrong_token(self, client):
        assert client.get("/", headers={"Authorization": "Bearer wrong"}).status_code == 401


class TestRequestHelpers:
    def test_request_source_ip_from_x_forwarded_for(self):
        from fastapi import Request

        mock_req = MagicMock(spec=Request)
        mock_req.headers = {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}
        from cerebellum.ui.dashboard import _request_source_ip

        assert _request_source_ip(mock_req) == "1.2.3.4"

    def test_request_source_ip_from_client(self):
        from fastapi import Request
        from starlette.datastructures import Address

        mock_req = MagicMock(spec=Request)
        mock_req.headers = {}
        mock_req.client = Address(host="10.0.0.1", port=12345)
        from cerebellum.ui.dashboard import _request_source_ip

        assert _request_source_ip(mock_req) == "10.0.0.1"

    def test_request_source_ip_unknown(self):
        from fastapi import Request

        mock_req = MagicMock(spec=Request)
        mock_req.headers = {}
        mock_req.client = None
        from cerebellum.ui.dashboard import _request_source_ip

        assert _request_source_ip(mock_req) == "unknown"

    def test_allow_authenticated_request_localhost(self):
        from cerebellum.ui.dashboard import _allow_authenticated_request

        assert _allow_authenticated_request("127.0.0.1") is True

    def test_allow_authenticated_request_loopback_ipv6(self):
        from cerebellum.ui.dashboard import _allow_authenticated_request

        assert _allow_authenticated_request("::1") is True

    def test_allow_authenticated_request_rate_limit(self, dash):
        from cerebellum.ui.dashboard import _allow_authenticated_request

        assert _allow_authenticated_request("192.168.1.100") is True
        now_window = int(time.time() // 60)
        with dash._auth_rate_lock:
            dash._auth_rate_windows["192.168.1.100"] = (now_window, 60)
        assert _allow_authenticated_request("192.168.1.100") is False


class TestDashboardDb:
    def test_get_dashboard_db_creates_tables(self, dash, monkeypatch, tmp_path):
        db_path = tmp_path / "test_dashboard.db"
        monkeypatch.setenv("CEREBELLUM_DASHBOARD_DB", str(db_path))
        original_conn = dash._dashboard_db
        dash._dashboard_db = None
        try:
            conn = dash._get_dashboard_db()
            assert conn is not None
            tables = [t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            assert "telegram_seen_updates" in tables
        finally:
            if dash._dashboard_db is not None and dash._dashboard_db is not original_conn:
                dash._dashboard_db.close()
            dash._dashboard_db = original_conn


class TestTelegramWebhook:
    def _webhook_headers(self):
        return {
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        }

    def test_webhook_bad_secret(self, client):
        payload = {"update_id": 1}
        response = client.post(
            "/telegram/webhook",
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert response.status_code == 401

    def test_webhook_valid_message(self, client):
        payload = {"update_id": 12345, "message": {"text": "/start", "chat": {"id": 12345678}}}
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_callback_query(self, client):
        import time as _time

        payload = {
            "update_id": 67890,
            "callback_query": {
                "id": "cb-123",
                "data": "approve:abc123",
                "from": {"id": 12345678},
                "message": {"date": int(_time.time())},
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_empty_payload(self, client):
        payload = {"update_id": 99999}
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_channel_post(self, client):
        payload = {"update_id": 22222, "channel_post": {"text": "channel msg", "chat": {"id": -100123, "type": "channel"}}}
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_poll_update(self, client):
        payload = {
            "update_id": 33333,
            "poll": {
                "id": "poll-1",
                "question": "Test?",
                "options": [{"text": "A", "voter_count": 1}],
                "is_closed": False,
                "is_anonymous": True,
                "type": "regular",
                "allows_multiple_answers": False,
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_duplicate_update_id(self, client):
        payload = {"update_id": 999999, "message": {"text": "/dup", "chat": {"id": 12345678}}}
        r1 = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert r1.status_code == 200
        r2 = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert r2.status_code == 200
        assert r2.json().get("message") == "update already processed"

    def test_webhook_callback_expired(self, client):
        payload = {
            "update_id": 888888,
            "callback_query": {
                "id": "cb-exp",
                "data": "approve:test-123",
                "from": {"id": 12345678},
                "message": {"date": 1000000},
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200
        assert response.json().get("message") == "callback expired"

    def test_webhook_unauthorized_user(self, client):
        payload = {
            "update_id": 777777,
            "callback_query": {
                "id": "cb-unauth",
                "data": "approve:test-123",
                "from": {"id": 99999999},
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 403

    def test_webhook_callback_approve(self, client, monkeypatch, dash):
        import time as _time

        mock_arbiter = MagicMock()
        mock_arbiter.handle_approval.return_value = {"status": "approved"}
        monkeypatch.setattr(dash, "get_arbiter", MagicMock(return_value=mock_arbiter))
        payload = {
            "update_id": 666666,
            "callback_query": {
                "id": "cb-approve",
                "data": "approve:abc123",
                "from": {"id": 12345678},
                "message": {"date": int(_time.time())},
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_callback_reject(self, client, monkeypatch, dash):
        import time as _time

        mock_arbiter = MagicMock()
        mock_arbiter.handle_approval.return_value = {"status": "rejected"}
        monkeypatch.setattr(dash, "get_arbiter", MagicMock(return_value=mock_arbiter))
        payload = {
            "update_id": 555555,
            "callback_query": {
                "id": "cb-reject",
                "data": "reject:abc123",
                "from": {"id": 12345678},
                "message": {"date": int(_time.time())},
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_callback_snooze(self, client, monkeypatch, dash):
        import time as _time

        mock_arbiter = MagicMock()
        mock_arbiter.handle_approval.return_value = {"status": "snoozed"}
        monkeypatch.setattr(dash, "get_arbiter", MagicMock(return_value=mock_arbiter))
        payload = {
            "update_id": 444444,
            "callback_query": {
                "id": "cb-snooze",
                "data": "snooze:abc123",
                "from": {"id": 12345678},
                "message": {"date": int(_time.time())},
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_message_text_command(self, client, monkeypatch, dash):
        mock_arbiter = MagicMock()
        mock_arbiter.kill_switch = False
        monkeypatch.setattr(dash, "get_arbiter", MagicMock(return_value=mock_arbiter))
        payload = {"update_id": 333333, "message": {"text": "/status", "chat": {"id": 12345678}, "from": {"id": 12345678}}}
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_message_no_text(self, client, monkeypatch, dash):
        mock_arbiter = MagicMock()
        mock_arbiter.kill_switch = False
        monkeypatch.setattr(dash, "get_arbiter", MagicMock(return_value=mock_arbiter))
        payload = {"update_id": 111111, "message": {"photo": [{"file_id": "test"}], "chat": {"id": 12345678}, "from": {"id": 12345678}}}
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_kill_switch_command(self, client, monkeypatch, dash):
        mock_arbiter = MagicMock()
        mock_arbiter.toggle_kill_switch.return_value = {"kill_switch": True}
        monkeypatch.setattr(dash, "get_arbiter", MagicMock(return_value=mock_arbiter))
        payload = {"update_id": 222222, "message": {"text": "/cerebellum-halt", "chat": {"id": 12345678}, "from": {"id": 12345678}}}
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 200

    def test_webhook_kill_switch_unauthorized(self, client):
        payload = {"update_id": 333333, "message": {"text": "/cerebellum-halt", "chat": {"id": 12345678}, "from": {"id": 99999999}}}
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 403

    def test_webhook_callback_invalid_hypothesis_id(self, client):
        import time as _time

        payload = {
            "update_id": 444444,
            "callback_query": {
                "id": "cb-invalid",
                "data": "approve:<script>alert(1)</script>",
                "from": {"id": 12345678},
                "message": {"date": int(_time.time())},
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 400

    def test_webhook_callback_unknown_action(self, client):
        import time as _time

        payload = {
            "update_id": 555555,
            "callback_query": {
                "id": "cb-unknown",
                "data": "unknown:abc123",
                "from": {"id": 12345678},
                "message": {"date": int(_time.time())},
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 400

    def test_webhook_callback_arbiter_unavailable(self, client, monkeypatch, dash):
        import time as _time

        monkeypatch.setattr(dash, "get_arbiter", MagicMock(return_value=None))
        payload = {
            "update_id": 666666,
            "callback_query": {
                "id": "cb-noarb",
                "data": "approve:abc123",
                "from": {"id": 12345678},
                "message": {"date": int(_time.time())},
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 503

    def test_webhook_callback_handler_exception(self, client, monkeypatch, dash):
        import time as _time

        mock_arbiter = MagicMock()
        mock_arbiter.handle_approval.side_effect = Exception("boom")
        monkeypatch.setattr(dash, "get_arbiter", MagicMock(return_value=mock_arbiter))
        payload = {
            "update_id": 777777,
            "callback_query": {
                "id": "cb-err",
                "data": "approve:abc123",
                "from": {"id": 12345678},
                "message": {"date": int(_time.time())},
            },
        }
        response = client.post("/telegram/webhook", content=json.dumps(payload).encode(), headers=self._webhook_headers())
        assert response.status_code == 500


class TestDashboardEndpoints:
    def test_api_stats_endpoint(self, client, monkeypatch, dash):
        mock_emitter = MagicMock()
        mock_emitter.query.return_value = []
        monkeypatch.setattr(dash, "get_emitter", MagicMock(return_value=mock_emitter))
        response = client.get("/api/stats", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200

    def test_api_stats_html_endpoint(self, client, monkeypatch, dash):
        mock_emitter = MagicMock()
        mock_emitter.query.return_value = []
        monkeypatch.setattr(dash, "get_emitter", MagicMock(return_value=mock_emitter))
        response = client.get("/api/stats/html", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200

    def test_api_events_endpoint(self, client, monkeypatch, dash):
        mock_emitter = MagicMock()
        mock_emitter.query.return_value = []
        monkeypatch.setattr(dash, "get_emitter", MagicMock(return_value=mock_emitter))
        response = client.get("/api/events", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200

    def test_timeline_endpoint(self, client, monkeypatch, dash):
        mock_emitter = MagicMock()
        mock_emitter.query.return_value = []
        monkeypatch.setattr(dash, "get_emitter", MagicMock(return_value=mock_emitter))
        response = client.get("/timeline", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200

    def test_root_endpoint(self, client):
        response = client.get("/", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200


class TestCoverageHelpers:
    def test_parse_since_valid(self):
        from cerebellum.ui.dashboard import _parse_since

        assert _parse_since("2024-01-01T00:00:00Z") is not None

    def test_parse_since_invalid(self):
        from cerebellum.ui.dashboard import _parse_since

        assert _parse_since("not-a-date") is None
        assert _parse_since("") is None
        assert _parse_since(None) is None

    def test_stats_payload_structure(self, monkeypatch):
        from cerebellum.ui.dashboard import _stats_payload

        with patch("cerebellum.ui.dashboard.get_emitter") as mock_get:
            mock_emitter = MagicMock()
            mock_emitter.query.return_value = []
            mock_get.return_value = mock_emitter
            result = _stats_payload()
            assert "window" in result
            assert "counts" in result
            assert "total" in result

    def test_render_events_safe(self):
        from cerebellum.ui.dashboard import _render_events

        events = [
            {
                "id": "test-id",
                "timestamp": "2024-01-01T00:00:00Z",
                "type": "test.event",
                "payload": {"text": "<script>alert(1)</script>"},
                "actor": "test",
                "context": {},
            }
        ]
        result = _render_events(events)
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_render_events_empty_list(self):
        from cerebellum.ui.dashboard import _render_events

        result = _render_events([])
        assert "No events yet" in result

    def test_render_events_with_none_payload(self):
        from cerebellum.ui.dashboard import _render_events

        events = [{"id": "t", "timestamp": "2024-01-01T00:00:00Z", "type": "t", "payload": None, "actor": "t", "context": {}}]
        result = _render_events(events)
        assert "t" in result


class TestTelegramHelpers:
    def test_answer_callback_logs_on_failure(self, monkeypatch):
        from cerebellum.ui.dashboard import _answer_callback

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
        _answer_callback("cb-123", "test answer")

    def test_send_telegram_text_logs_on_failure(self, monkeypatch):
        from cerebellum.ui.dashboard import _send_telegram_text

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
        _send_telegram_text(12345678, "test message")


class TestBaseDirHelpers:
    def test_base_dir_from_env(self, monkeypatch):
        from cerebellum.ui.dashboard import _base_dir

        monkeypatch.setenv("CEREBELLUM_BASE_DIR", "/tmp/test-cerebellum")
        result = _base_dir()
        assert "/tmp/test-cerebellum" in str(result)

    def test_config_path_from_env(self, monkeypatch):
        from cerebellum.ui.dashboard import _config_path

        monkeypatch.setenv("CEREBELLUM_CONFIG", "/tmp/test-config.json")
        result = _config_path()
        assert "/tmp/test-config.json" in str(result)
