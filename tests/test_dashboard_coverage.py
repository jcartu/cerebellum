"""Tests for Phase 6: dashboard coverage boost.

Targets: dashboard.py 20% -> 75%.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
import sys

for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("DASHBOARD_TOKEN", "test-token")

from fastapi.testclient import TestClient

# Import dashboard module
from cerebellum.ui import dashboard

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_path() -> str:
    """Create a temporary config.json and return its path."""
    content = {
        "sqlite": {"events_db": ":memory:"},
        "dashboard": {"port": 18790},
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(content, f)
    return path


def _setup_dashboard_env(config_path: str) -> None:
    """Set up environment for dashboard tests."""
    os.environ["CEREBELLUM_CONFIG"] = config_path
    base = str(Path(config_path).parent)
    os.environ["CEREBELLUM_BASE_DIR"] = base


def _get_client(config_path: str | None = None) -> TestClient:
    """Create a TestClient for the dashboard app."""
    if config_path is None:
        config_path = _make_config_path()
    _setup_dashboard_env(config_path)

    # Reset singletons
    dashboard._emitter = None
    dashboard._arbiter = None
    dashboard._feedback_store = None
    dashboard._dashboard_db = None

    # Mock emitter
    mock_emitter = MagicMock()
    mock_emitter.query.return_value = []
    with patch.object(dashboard, "get_emitter", return_value=mock_emitter):
        client = TestClient(dashboard.app)
        return client


# ---------------------------------------------------------------------------
# healthz (no auth)
# ---------------------------------------------------------------------------


class TestHealthz:
    def test_healthz(self) -> None:
        client = _get_client()
        response = client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class TestAuthMiddleware:
    def test_requires_auth(self) -> None:
        client = _get_client()
        response = client.get("/")
        assert response.status_code == 401

    def test_auth_with_token(self) -> None:
        client = _get_client()
        response = client.get("/", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200

    def test_wrong_token(self) -> None:
        client = _get_client()
        response = client.get("/", headers={"Authorization": "Bearer wrong-token"})
        assert response.status_code == 401

    def test_healthz_no_auth(self) -> None:
        client = _get_client()
        response = client.get("/healthz")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class TestPages:
    def test_index_page(self) -> None:
        client = _get_client()
        response = client.get("/", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        assert "CEREBELLUM" in response.text

    def test_timeline_page(self) -> None:
        client = _get_client()
        response = client.get("/timeline", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200

    def test_timeline_limit(self) -> None:
        client = _get_client()
        response = client.get("/timeline?limit=10", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


class TestAPIEndpoints:
    def test_api_events(self) -> None:
        client = _get_client()
        response = client.get("/api/events", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_api_events_with_since(self) -> None:
        client = _get_client()
        response = client.get(
            "/api/events?since=2025-01-01T00:00:00Z",
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200

    def test_api_stats(self) -> None:
        client = _get_client()
        response = client.get("/api/stats", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        data = response.json()
        assert "window" in data
        assert "counts" in data
        assert "total" in data

    def test_api_stats_html(self) -> None:
        client = _get_client()
        response = client.get("/api/stats/html", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        assert "24h Stats" in response.text


# ---------------------------------------------------------------------------
# Metrics endpoints
# ---------------------------------------------------------------------------


class TestMetricsEndpoints:
    def test_metrics_page(self) -> None:
        client = _get_client()
        with patch.object(dashboard, "get_feedback_store") as mock_store:
            mock_metrics = MagicMock()
            mock_metrics.model = "test-model"
            mock_metrics.window_days = 7
            mock_metrics.total_outcomes = 0
            mock_metrics.approval_rate = 0.0
            mock_metrics.mean_confidence_approved = 0.0
            mock_metrics.mean_confidence_rejected = 0.0
            mock_metrics.expected_calibration_error = 0.0
            mock_metrics.is_calibrated = True
            mock_store.return_value.compute_calibration.return_value = mock_metrics
            mock_store.return_value.query_outcomes.return_value = []
            response = client.get("/metrics", headers={"Authorization": "Bearer test-token"})
            assert response.status_code == 200

    def test_api_metrics(self) -> None:
        client = _get_client()
        with patch.object(dashboard, "get_feedback_store") as mock_store:
            mock_metrics = MagicMock()
            mock_metrics.model = "test-model"
            mock_metrics.window_days = 7
            mock_metrics.total_outcomes = 0
            mock_metrics.approval_rate = 0.0
            mock_metrics.mean_confidence_approved = 0.0
            mock_metrics.mean_confidence_rejected = 0.0
            mock_metrics.expected_calibration_error = 0.0
            mock_metrics.is_calibrated = True
            mock_metrics.platt_a = 1.0
            mock_metrics.platt_b = 0.0
            mock_store.return_value.compute_calibration.return_value = mock_metrics
            response = client.get("/api/metrics", headers={"Authorization": "Bearer test-token"})
            assert response.status_code == 200
            data = response.json()
            assert "model" in data
            assert "is_calibrated" in data


# ---------------------------------------------------------------------------
# Dashboard helper functions
# ---------------------------------------------------------------------------


class TestDashboardHelpers:
    def test_stats_payload(self) -> None:
        mock_emitter = MagicMock()
        mock_emitter.query.return_value = [
            {"type": "cerebellum.action"},
            {"type": "cerebellum.action"},
            {"type": "cerebellum.execution"},
        ]
        with patch.object(dashboard, "get_emitter", return_value=mock_emitter):
            payload = dashboard._stats_payload()
            assert payload["total"] == 3
            assert payload["window"] == "24h"

    def test_parse_since_valid(self) -> None:
        result = dashboard._parse_since("2025-01-01T00:00:00+00:00")
        assert result is not None

    def test_parse_since_invalid(self) -> None:
        result = dashboard._parse_since("not-a-date")
        assert result is None

    def test_parse_since_none(self) -> None:
        result = dashboard._parse_since(None)
        assert result is None

    def test_render_events_empty(self) -> None:
        html = dashboard._render_events([])
        assert "No events yet" in html

    def test_render_events_with_events(self) -> None:
        events = [
            {
                "id": "evt-1",
                "type": "cerebellum.action",
                "timestamp": "2025-01-01T00:00:00",
                "actor": "test",
                "payload": {"key": "value"},
                "context": {},
            }
        ]
        html = dashboard._render_events(events)
        assert "evt-1" in html
        assert "cerebellum.action" in html

    def test_request_source_ip(self) -> None:
        mock_request = MagicMock()
        mock_request.headers = {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}
        mock_request.client = None
        ip = dashboard._request_source_ip(mock_request)
        assert ip == "1.2.3.4"

    def test_request_source_ip_no_forwarded(self) -> None:
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_client = MagicMock()
        mock_client.host = "127.0.0.1"
        mock_request.client = mock_client
        ip = dashboard._request_source_ip(mock_request)
        assert ip == "127.0.0.1"

    def test_allow_telegram_webhook_ip(self) -> None:
        result = dashboard._allow_telegram_webhook_ip("149.154.160.0")
        assert isinstance(result, bool)

    def test_allow_authenticated_request(self) -> None:
        result = dashboard._allow_authenticated_request("127.0.0.1")
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------


class TestTelegramWebhook:
    def test_webhook_no_bot_token(self) -> None:
        client = _get_client()
        env = dict(os.environ)
        env.pop("TELEGRAM_BOT_TOKEN", None)
        env.pop("OPENCLAW_TELEGRAM_BOT_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            # Need to reload the module to pick up new env
            import importlib
            importlib.reload(dashboard)
            client = TestClient(dashboard.app)
            response = client.post("/telegram/webhook", json={"update_id": 1})
            assert response.status_code == 503

    def test_webhook_no_secret(self) -> None:
        client = _get_client()
        env = dict(os.environ)
        env["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
        env.pop("TELEGRAM_WEBHOOK_SECRET", None)
        with patch.dict(os.environ, env, clear=True):
            import importlib
            importlib.reload(dashboard)
            client = TestClient(dashboard.app)
            response = client.post(
                "/telegram/webhook",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            )
            assert response.status_code == 503

    def test_webhook_bad_json(self) -> None:
        client = _get_client()
        env = dict(os.environ)
        env["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
        env["TELEGRAM_WEBHOOK_SECRET"] = "test-secret"
        env["TELEGRAM_ALLOWED_USER_IDS"] = "12345"
        with patch.dict(os.environ, env, clear=True):
            import importlib
            importlib.reload(dashboard)
            client = TestClient(dashboard.app)
            response = client.post(
                "/telegram/webhook",
                content=b"not json",
                headers={
                    "Content-Type": "application/json",
                    "X-Telegram-Bot-Api-Secret-Token": "test-secret",
                },
            )
            assert response.status_code == 400

    def test_webhook_bad_secret(self) -> None:
        client = _get_client()
        env = dict(os.environ)
        env["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
        env["TELEGRAM_WEBHOOK_SECRET"] = "correct-secret"
        env["TELEGRAM_ALLOWED_USER_IDS"] = "12345"
        with patch.dict(os.environ, env, clear=True):
            import importlib
            importlib.reload(dashboard)
            client = TestClient(dashboard.app)
            response = client.post(
                "/telegram/webhook",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            )
            assert response.status_code == 401

    def test_webhook_duplicate_update(self) -> None:
        client = _get_client()
        env = dict(os.environ)
        env["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
        env["TELEGRAM_WEBHOOK_SECRET"] = "test-secret"
        env["TELEGRAM_ALLOWED_USER_IDS"] = "12345"
        with patch.dict(os.environ, env, clear=True):
            import importlib
            importlib.reload(dashboard)
            client = TestClient(dashboard.app)
            headers = {
                "X-Telegram-Bot-Api-Secret-Token": "test-secret",
            }
            payload = {"update_id": "dup-1"}
            r1 = client.post("/telegram/webhook", json=payload, headers=headers)
            assert r1.status_code == 200
            r2 = client.post("/telegram/webhook", json=payload, headers=headers)
            assert r2.json().get("message") == "update already processed"


# ---------------------------------------------------------------------------
# get_arbiter / get_emitter
# ---------------------------------------------------------------------------


class TestSingletons:
    def test_get_emitter(self) -> None:
        dashboard._emitter = None
        mock_emitter = MagicMock()
        with patch.object(dashboard, "EventBus", return_value=mock_emitter):
            result = dashboard.get_emitter()
            assert result is mock_emitter

    def test_get_arbiter_no_policy(self) -> None:
        dashboard._arbiter = None
        with patch.object(Path, "exists", return_value=False):
            result = dashboard.get_arbiter()
            assert result is None

    def test_get_feedback_store(self) -> None:
        dashboard._feedback_store = None
        with patch.object(dashboard, "FeedbackStore") as _mock_store:
            result = dashboard.get_feedback_store()
            assert result is not None


# ---------------------------------------------------------------------------
# Rate limiting helpers
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_telegram_rate_limit(self) -> None:
        # After 60 requests in a minute window, should be rejected
        for i in range(60):
            dashboard._allow_telegram_webhook_ip("1.2.3.4")
        result = dashboard._allow_telegram_webhook_ip("1.2.3.4")
        assert result is False

    def test_auth_rate_limit(self) -> None:
        for i in range(60):
            dashboard._allow_authenticated_request("5.6.7.8")
        result = dashboard._allow_authenticated_request("5.6.7.8")
        assert result is False

# ---------------------------------------------------------------------------
# Telegram callback helpers
# ---------------------------------------------------------------------------


class TestTelegramHelpers:
    def test_answer_callback(self) -> None:
        with patch("cerebellum.ui.dashboard._safe_opener"):
            dashboard._answer_callback("cb-1", "test message")

    def test_send_telegram_text(self) -> None:
        with patch("cerebellum.ui.dashboard._safe_opener"):
            dashboard._send_telegram_text("chat-123", "hello")

    def test_answer_callback_error(self) -> None:
        import urllib.error as urllib_error
        with patch("cerebellum.ui.dashboard._safe_opener", side_effect=urllib_error.URLError("fail")):
            # Should not raise - catches internally
            dashboard._answer_callback("cb-2", "test")

    def test_send_telegram_text_error(self) -> None:
        import urllib.error as urllib_error
        with patch("cerebellum.ui.dashboard._safe_opener", side_effect=urllib_error.URLError("fail")):
            dashboard._send_telegram_text("chat-123", "hello")


# ---------------------------------------------------------------------------
# Dashboard webhook callback handling (module-level tests)
# ---------------------------------------------------------------------------


class TestWebhookCallback:
    """Test webhook callback handling using module-level state manipulation.

    Note: We cannot reload the dashboard module with TestClient because
    FastAPI binds routes at module import time. These tests manipulate
    the module-level state directly instead.
    """

    def test_telegram_allowed_user_ids_is_set(self) -> None:
        from cerebellum.ui import dashboard as dash
        assert isinstance(dash.TELEGRAM_ALLOWED_USER_IDS, set)

    def test_hypothesis_id_regex(self) -> None:
        from cerebellum.ui import dashboard as dash
        assert dash.HYPOTHESIS_ID_RE.match("h-123") is not None
        assert dash.HYPOTHESIS_ID_RE.match("<script>") is None
        assert dash.HYPOTHESIS_ID_RE.match("a" * 200) is None
