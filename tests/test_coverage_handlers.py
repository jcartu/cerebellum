"""Coverage gap tests - policy_arbiter handlers, helpers, and edge cases.

Targets the largest missing-line clusters in policy_arbiter.py and event_bus.py
to push global coverage from ~77% to 80%.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
    from cerebellum.event_bus import EventBus
    eb = EventBus(config_path)
    yield eb
    eb.close()


@pytest.fixture()
def arbiter_policy_path(tmp_path: Path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "global:\n"
        "  enabled: true\n"
        "  max_actions_per_hour: 10\n"
        "  max_llm_cost_per_day_usd: 5.0\n"
        "allowed_roots:\n"
        f"  - {tmp_path}\n"
        "model_candidates:\n"
        "  - openai/gpt-4o\n"
        "auto_execute:\n"
        "  min_confidence: 0.85\n"
        "  max_cost: 0.3\n"
        "  required_reversibility: [reversible]\n"
        "  allowed_tools: [file.read]\n"
        "stage_notify:\n"
        "  min_confidence: 0.6\n"
        "  max_cost: 0.8\n"
        "discard:\n"
        "  max_confidence: 0.5\n"
        "  min_cost: 0.9\n"
    )
    return policy


@pytest.fixture()
def arbiter(arbiter_policy_path, emitter, tmp_path, monkeypatch):
    monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
    monkeypatch.setenv("CEREBELLUM_BASE_DIR", str(tmp_path))
    from cerebellum.policy_arbiter import PolicyArbiter
    arb = PolicyArbiter(str(arbiter_policy_path), emitter=emitter)
    return arb


# ---------------------------------------------------------------------------
# RateLimiter edge cases
# ---------------------------------------------------------------------------


class TestRateLimiterEdgeCases:
    def test_prune_removes_old_events(self, arbiter):
        from cerebellum.policy_arbiter import RateLimiter
        rl = RateLimiter(max_count=5, window_seconds=1)
        import time
        rl.events.append(time.monotonic() - 10)
        rl.events.append(time.monotonic() - 10)
        rl._prune(time.monotonic())
        assert len(rl.events) == 0

    def test_snapshot(self, arbiter):
        from cerebellum.policy_arbiter import RateLimiter
        rl = RateLimiter(max_count=3, window_seconds=60)
        rl.allow()
        snapshot = rl.snapshot()
        assert snapshot["used"] == 1
        assert snapshot["remaining"] == 2
        assert snapshot["max_count"] == 3

    def test_allow_rejects_when_full(self, arbiter):
        from cerebellum.policy_arbiter import RateLimiter
        rl = RateLimiter(max_count=2, window_seconds=60)
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is False


# ---------------------------------------------------------------------------
# DailyCostTracker edge cases
# ---------------------------------------------------------------------------


class TestDailyCostTrackerEdgeCases:
    def test_allow_rejects_over_budget(self, arbiter):
        from cerebellum.policy_arbiter import DailyCostTracker
        dct = DailyCostTracker(max_cost=1.0)
        assert dct.allow(0.6) is True
        assert dct.allow(0.6) is False

    def test_negative_cost_clamped_to_zero(self, arbiter):
        from cerebellum.policy_arbiter import DailyCostTracker
        dct = DailyCostTracker(max_cost=1.0)
        assert dct.allow(-5.0) is True
        assert dct.allow(0.5) is True

    def test_snapshot(self, arbiter):
        from cerebellum.policy_arbiter import DailyCostTracker
        dct = DailyCostTracker(max_cost=10.0)
        dct.allow(3.0)
        snap = dct.snapshot()
        assert snap["spent"] == 3.0
        assert snap["remaining"] == 7.0


# ---------------------------------------------------------------------------
# Policy load failure
# ---------------------------------------------------------------------------


class TestPolicyLoadFailure:
    def test_policy_load_failure(self, emitter, tmp_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "")
        monkeypatch.setenv("CEREBELLUM_BASE_DIR", str(tmp_path))
        from cerebellum.policy_arbiter import PolicyArbiter
        bad_policy = tmp_path / "bad_policy.yaml"
        bad_policy.write_text(":::\n  - [invalid yaml")
        with pytest.raises(RuntimeError, match="Unable to load arbiter policy"):
            PolicyArbiter(str(bad_policy), emitter=emitter)


# ---------------------------------------------------------------------------
# Evaluation edge cases
# ---------------------------------------------------------------------------


class TestEvaluationEdgeCases:
    def test_evaluation_discard_below_thresholds(self, arbiter):
        decision = arbiter.evaluate({
            "id": "eval-low",
            "confidence": 0.3,
            "generation_cost_usd": 0.1,
            "tools": [{"tool": "file.read", "path": "/tmp/x"}],
            "reversibility": "reversible",
        })
        assert decision.decision == "discard"

    def test_evaluation_discard_high_cost(self, arbiter):
        decision = arbiter.evaluate({
            "id": "eval-expensive",
            "confidence": 0.9,
            "generation_cost_usd": 1.0,
            "tools": [{"tool": "file.read", "path": "/tmp/x"}],
            "reversibility": "reversible",
        })
        assert decision.decision == "discard"

    def test_evaluation_missing_confidence(self, arbiter):
        decision = arbiter.evaluate({
            "id": "eval-no-conf",
            "generation_cost_usd": 0.1,
            "tools": [{"tool": "file.read", "path": "/tmp/x"}],
            "reversibility": "reversible",
        })
        assert decision.decision == "discard"

    def test_evaluation_non_finite_confidence(self, arbiter):
        decision = arbiter.evaluate({
            "id": "eval-inf",
            "confidence": float("inf"),
            "generation_cost_usd": 0.1,
            "tools": [{"tool": "file.read", "path": "/tmp/x"}],
            "reversibility": "reversible",
        })
        assert decision.decision == "discard"

    def test_evaluation_negative_confidence(self, arbiter):
        decision = arbiter.evaluate({
            "id": "eval-neg",
            "confidence": -0.5,
            "generation_cost_usd": 0.1,
            "tools": [{"tool": "file.read", "path": "/tmp/x"}],
            "reversibility": "reversible",
        })
        assert decision.decision == "discard"

    def test_evaluation_invalid_generation_cost(self, arbiter):
        decision = arbiter.evaluate({
            "id": "eval-bad-cost",
            "confidence": 0.9,
            "generation_cost_usd": "not_a_number",
            "tools": [{"tool": "file.read", "path": "/tmp/x"}],
            "reversibility": "reversible",
        })
        assert decision.decision == "discard"

    def test_evaluation_non_allowed_tools(self, arbiter):
        decision = arbiter.evaluate({
            "id": "eval-non-allowed",
            "confidence": 0.9,
            "generation_cost_usd": 0.1,
            "plan": [{"tool": "dangerous.tool"}],
            "reversibility": "reversible",
        })
        assert decision.decision in ("stage_notify", "discard")


# ---------------------------------------------------------------------------
# Handler tests - http.get
# ---------------------------------------------------------------------------


class TestHandleHttpGet:
    def test_http_get_missing_url(self, arbiter):
        with pytest.raises(ValueError, match="http.get requires a url"):
            arbiter._handle_http_get({})

    def test_http_get_valid_url(self, arbiter):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.build_opener") as mock_opener:
            mock_opener.return_value.open = MagicMock(return_value=mock_response)
            with patch.object(arbiter, "_validate_url", return_value=("1.2.3.4", "example.com")):
                result = arbiter._handle_http_get({"url": "http://example.com"})
                assert result["status"] == "ok"
                assert result["http_status"] == 200

    def test_http_get_https_path(self, arbiter):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.build_opener") as mock_opener:
            mock_opener.return_value.open = MagicMock(return_value=mock_response)
            with patch.object(arbiter, "_validate_url", return_value=("1.2.3.4", "example.com")):
                result = arbiter._handle_http_get({"url": "https://example.com/api"})
                assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Handler tests - web.search
# ---------------------------------------------------------------------------


class TestHandleWebSearch:
    def test_web_search_missing_query(self, arbiter):
        with pytest.raises(ValueError, match="web.search requires a query"):
            arbiter._handle_web_search({})

    def test_web_search_no_api_key(self, arbiter, monkeypatch):
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="BRAVE_SEARCH_API_KEY is not configured"):
            arbiter._handle_web_search({"query": "test"})


# ---------------------------------------------------------------------------
# Handler tests - file.read
# ---------------------------------------------------------------------------


class TestHandleFileRead:
    def test_file_read_missing_path(self, arbiter):
        with pytest.raises(ValueError):
            arbiter._handle_file_read({})

    def test_file_read_success(self, arbiter, tmp_path):
        test_file = tmp_path / "readme.txt"
        test_file.write_text("hello world")
        result = arbiter._handle_file_read({"path": str(test_file)})
        assert result["status"] == "ok"
        assert result["content"] == "hello world"
        assert result["truncated"] is False

    def test_file_read_truncation(self, arbiter, tmp_path):
        test_file = tmp_path / "large.txt"
        test_file.write_text("x" * 15000)
        result = arbiter._handle_file_read({"path": str(test_file)})
        assert len(result["content"]) == 10000
        assert result["truncated"] is True

    def test_file_read_via_file_key(self, arbiter, tmp_path):
        test_file = tmp_path / "data.txt"
        test_file.write_text("data")
        result = arbiter._handle_file_read({"file": str(test_file)})
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Handler tests - memory.query
# ---------------------------------------------------------------------------


class TestHandleMemoryQuery:
    def test_memory_query_non_localhost(self, arbiter, monkeypatch):
        monkeypatch.setenv("QDRANT_URL", "http://evil.example.com:6333")
        with pytest.raises(ValueError, match="Qdrant URL must be localhost"):
            arbiter._handle_memory_query({"collection": "test", "vector": [0.1]})

    def test_memory_query_invalid_collection(self, arbiter, monkeypatch):
        monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:6333")
        with pytest.raises(ValueError, match="Invalid Qdrant collection name"):
            arbiter._handle_memory_query({"collection": "drop table", "vector": [0.1]})

    def test_memory_query_missing_vector(self, arbiter, monkeypatch):
        monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:6333")
        with pytest.raises(ValueError, match="memory.query requires a vector payload"):
            arbiter._handle_memory_query({"collection": "test"})


# ---------------------------------------------------------------------------
# Handler tests - model.call
# ---------------------------------------------------------------------------


class TestHandleModelCall:
    def test_model_call_no_api_key(self, arbiter, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not configured"):
            arbiter._handle_model_call({"model": "openai/gpt-4o"})

    def test_model_call_unauthorized_model(self, arbiter, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        with pytest.raises(ValueError, match="not in allowlist"):
            arbiter._handle_model_call({"model": "unknown/model"})


# ---------------------------------------------------------------------------
# Handler tests - notification.send
# ---------------------------------------------------------------------------


class TestHandleNotificationSend:
    def test_notification_send_missing_text(self, arbiter):
        with pytest.raises(ValueError, match="notification.send requires text"):
            arbiter._handle_notification_send({})


# ---------------------------------------------------------------------------
# Handler tests - notification.summarize
# ---------------------------------------------------------------------------


class TestHandleNotificationSummarize:
    def test_notification_summarize_with_emitter(self, arbiter):
        result = arbiter._handle_notification_summarize({"hours": 1})
        assert isinstance(result, dict)
        assert result["tool"] == "notification.summarize"

    def test_notification_summarize_no_events(self, arbiter):
        result = arbiter._handle_notification_summarize({"hours": 0.001})
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# _site_url fallback paths
# ---------------------------------------------------------------------------


class TestSiteUrlFallbacks:
    def test_site_url_from_env(self, arbiter, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_SITE_URL", "https://env.example.com")
        monkeypatch.delenv("CEREBELLUM_HTTP_REFERER", raising=False)
        monkeypatch.delenv("OPENROUTER_HTTP_REFERER", raising=False)
        result = arbiter._site_url()
        assert result == "https://env.example.com"

    def test_site_url_from_http_referer_env(self, arbiter, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_HTTP_REFERER", "https://referer-env.com")
        result = arbiter._site_url()
        assert result == "https://referer-env.com"

    def test_site_url_from_config(self, arbiter, monkeypatch):
        monkeypatch.delenv("CEREBELLUM_SITE_URL", raising=False)
        monkeypatch.delenv("CEREBELLUM_HTTP_REFERER", raising=False)
        monkeypatch.delenv("OPENROUTER_HTTP_REFERER", raising=False)
        arbiter.runtime_config["site_url"] = "https://config.example.com"
        result = arbiter._site_url()
        assert result == "https://config.example.com"

    def test_site_url_from_http_referer_config(self, arbiter, monkeypatch):
        monkeypatch.delenv("CEREBELLUM_SITE_URL", raising=False)
        monkeypatch.delenv("CEREBELLUM_HTTP_REFERER", raising=False)
        monkeypatch.delenv("OPENROUTER_HTTP_REFERER", raising=False)
        arbiter.runtime_config.pop("site_url", None)
        arbiter.runtime_config["http_referer"] = "https://referer-config.com"
        result = arbiter._site_url()
        assert result == "https://referer-config.com"

    def test_site_url_from_openrouter_config(self, arbiter, monkeypatch):
        monkeypatch.delenv("CEREBELLUM_SITE_URL", raising=False)
        monkeypatch.delenv("CEREBELLUM_HTTP_REFERER", raising=False)
        monkeypatch.delenv("OPENROUTER_HTTP_REFERER", raising=False)
        arbiter.runtime_config.pop("http_referer", None)
        arbiter.runtime_config.pop("site_url", None)
        arbiter.runtime_config["openrouter"] = {"http_referer": "https://referer-or.com"}
        result = arbiter._site_url()
        assert result == "https://referer-or.com"

    def test_site_url_default(self, arbiter, monkeypatch):
        monkeypatch.delenv("CEREBELLUM_SITE_URL", raising=False)
        monkeypatch.delenv("CEREBELLUM_HTTP_REFERER", raising=False)
        monkeypatch.delenv("OPENROUTER_HTTP_REFERER", raising=False)
        arbiter.runtime_config.pop("site_url", None)
        arbiter.runtime_config.pop("http_referer", None)
        arbiter.runtime_config.pop("openrouter", None)
        result = arbiter._site_url()
        assert result == "https://openclaw.local/cerebellum"


# ---------------------------------------------------------------------------
# _telegram_fallback_binary
# ---------------------------------------------------------------------------


class TestTelegramFallbackBinary:
    def test_fallback_binary_from_config(self, arbiter):
        arbiter.runtime_config["telegram"] = {"fallback_binary": "/usr/local/bin/openclaw"}
        result = arbiter._telegram_fallback_binary()
        assert result == "/usr/local/bin/openclaw"

class TestSanitizeHypothesis:
    def test_sanitize_removes_sensitive_keys(self, arbiter):
        result = arbiter._sanitize_hypothesis({
            "id": "test",
            "api_key": "secret",
            "password": "secret",
            "token": "secret",
            "secret": "secret",
            "normal_key": "normal_value",
        })
        assert result["api_key"] == "[REDACTED]"
        assert result["password"] == "[REDACTED]"
        assert result["token"] == "[REDACTED]"
        assert result["secret"] == "[REDACTED]"
        assert result["normal_key"] == "normal_value"

    def test_sanitize_with_nested_dict(self, arbiter):
        result = arbiter._sanitize_hypothesis({
            "id": "test",
            "config": {"api_key": "secret", "name": "visible"},
        })
        assert result["config"]["api_key"] == "[REDACTED]"
        assert result["config"]["name"] == "visible"


# ---------------------------------------------------------------------------
# _format_telegram_card
# ---------------------------------------------------------------------------


class TestFormatTelegramCard:
    def test_format_telegram_card_basic(self, arbiter):
        text = arbiter._format_telegram_card({
            "id": "hyp-1",
            "confidence": 0.9,
            "tools": [{"tool": "file.read"}],
        })
        assert "hyp-1" in text
        assert "0.9" in text

    def test_format_telegram_card_no_tools(self, arbiter):
        text = arbiter._format_telegram_card({
            "id": "hyp-2",
            "confidence": 0.5,
        })
        assert "hyp-2" in text


# ---------------------------------------------------------------------------
# _telegram_keyboard
# ---------------------------------------------------------------------------


class TestTelegramKeyboard:
    def test_telegram_keyboard(self, arbiter):
        keyboard = arbiter._telegram_keyboard("hyp-123")
        assert isinstance(keyboard, list)
        assert len(keyboard) > 0


# ---------------------------------------------------------------------------
# _send_telegram_message - openclaw fallback path
# ---------------------------------------------------------------------------


class TestSendTelegramMessageOpenclaw:
    def test_telegram_no_credentials_no_openclaw(self, arbiter, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("OPENCLAW_TELEGRAM_CHAT_ID", raising=False)
        arbiter.runtime_config.pop("telegram", None)
        with patch.object(arbiter, "_resolve_openclaw_binary", return_value=None):
            result = arbiter._send_telegram_message("test message")
            assert result["ok"] is False
            assert result.get("skipped") is True
# ---------------------------------------------------------------------------
# _resolve_openclaw_binary
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# _is_world_writable_path
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# _update_hypothesis_state with cortex
# ---------------------------------------------------------------------------


class TestUpdateHypothesisState:
    def test_update_hypothesis_state_with_cortex(self, arbiter):
        mock_cortex = MagicMock()
        mock_cortex.update_hypothesis_state = MagicMock()
        arbiter.cortex = mock_cortex
        arbiter._update_hypothesis_state("hyp-1", "completed", {"result": "ok"})
        mock_cortex.update_hypothesis_state.assert_called_once_with("hyp-1", "completed", {"result": "ok"})

    def test_update_hypothesis_state_cortex_type_error_fallback(self, arbiter):
        mock_cortex = MagicMock()
        mock_cortex.update_hypothesis_state = MagicMock(side_effect=TypeError("wrong sig"))
        arbiter.cortex = mock_cortex
        arbiter._update_hypothesis_state("hyp-2", "completed", {"result": "ok"})

    def test_update_hypothesis_state_cortex_no_method(self, arbiter):
        mock_cortex = MagicMock(spec=[])
        arbiter.cortex = mock_cortex
        arbiter._update_hypothesis_state("hyp-3", "completed", {"result": "ok"})


# ---------------------------------------------------------------------------
# _emit_event with emitter
# ---------------------------------------------------------------------------


class TestEmitEvent:
    def test_emit_event_with_emitter_emit(self, arbiter):
        mock_emitter = MagicMock()
        mock_emitter.emit = MagicMock()
        arbiter.emitter = mock_emitter
        arbiter._emit_event("test.topic", {"data": "val"})
        mock_emitter.emit.assert_called_once_with("test.topic", {"data": "val"})

    def test_emit_event_with_emitter_publish(self, arbiter):
        mock_emitter = MagicMock(spec=["publish"])
        mock_emitter.publish = MagicMock()
        arbiter.emitter = mock_emitter
        arbiter._emit_event("test.topic2", {"data": "val2"})
        mock_emitter.publish.assert_called_once_with("test.topic2", {"data": "val2"})

    def test_emit_event_emitter_typeerror_fallback(self, arbiter):
        mock_emitter = MagicMock()
        def emit_fallback(*args):
            if len(args) == 2:
                raise TypeError("wrong sig")
            # Single arg call succeeds
        mock_emitter.emit = MagicMock(side_effect=emit_fallback)
        arbiter.emitter = mock_emitter
        arbiter._emit_event("test.topic3", {"data": "val3"})
        mock_emitter.emit.assert_any_call({"data": "val3"})

    def test_emit_event_no_emitter(self, arbiter):
        arbiter.emitter = None
        arbiter._emit_event("test.topic4", {"data": "val4"})
        fallback = arbiter.state_dir / "arbiter_fallback_events.jsonl"
        assert fallback.exists()


# ---------------------------------------------------------------------------
# _load_state with malformed decisions
# ---------------------------------------------------------------------------


class TestLoadState:
    def test_load_state_malformed_decision(self, arbiter, tmp_path):
        state_file = arbiter.state_dir / "arbiter_state.json"
        state_file.write_text(json.dumps({
            "kill_switch": False,
            "recent_decisions": [
                {"hypothesis_id": "good", "decision": "approve", "reason": "ok", "timestamp": "2025-01-01T00:00:00"},
                "not_a_dict",
                42,
            ],
        }))
        arbiter._load_state()
        assert len(arbiter.recent_decisions) == 1


# ---------------------------------------------------------------------------
# _save_json exception path
# ---------------------------------------------------------------------------


class TestSaveJsonException:
    def test_save_json_permission_error(self, arbiter, tmp_path):
        bad_path = tmp_path / "nonexistent" / "data.json"
        with pytest.raises(Exception):
            arbiter._save_json(bad_path, {"data": "val"})


# ---------------------------------------------------------------------------
# _coerce_optional_float
# ---------------------------------------------------------------------------


class TestCoerceOptionalFloat:
    def test_coerce_none(self, arbiter):
        assert arbiter._coerce_optional_float(None) is None

    def test_coerce_valid(self, arbiter):
        assert arbiter._coerce_optional_float("3.14") == 3.14

    def test_coerce_invalid_string(self, arbiter):
        assert arbiter._coerce_optional_float("abc") is None

    def test_coerce_infinity(self, arbiter):
        assert arbiter._coerce_optional_float(float("inf")) is None

    def test_coerce_negative(self, arbiter):
        assert arbiter._coerce_optional_float(-1.0) is None

    def test_coerce_zero(self, arbiter):
        assert arbiter._coerce_optional_float(0.0) == 0.0


# ---------------------------------------------------------------------------
# _extract_execution_cost
# ---------------------------------------------------------------------------


class TestExtractExecutionCost:
    def test_extract_cost_from_result(self, arbiter):
        cost = arbiter._extract_execution_cost({}, {"cost": 1.5})
        assert cost == 1.5

    def test_extract_cost_from_step(self, arbiter):
        cost = arbiter._extract_execution_cost({"execution_cost": 2.0}, {})
        assert cost == 2.0

    def test_extract_cost_non_numeric(self, arbiter):
        cost = arbiter._extract_execution_cost({}, {"cost": "free"})
        assert cost == 0.0

    def test_extract_cost_negative_clamped(self, arbiter):
        cost = arbiter._extract_execution_cost({}, {"cost": -5.0})
        assert cost == 0.0


# ---------------------------------------------------------------------------
# Event bus - NATS publish/subscribe paths
# ---------------------------------------------------------------------------


class TestEventBusNATSPublish:
    @pytest.mark.asyncio
    async def test_publish_event_no_jetstream(self, emitter):
        emitter._js = None
        await emitter._publish_event({"type": "test", "id": "1"})

    @pytest.mark.asyncio
    async def test_publish_event_success(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "test-token")
        mock_nats = MagicMock()
        mock_nats.connect = AsyncMock()
        mock_js = AsyncMock()
        mock_nats.jetstream = MagicMock(return_value=mock_js)
        mock_js.stream_info = AsyncMock()
        mock_js.publish = AsyncMock()

        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            with patch("cerebellum.event_bus.NATS", return_value=mock_nats):
                eb._nats_ready = False
                eb._nc = None
                eb._js = None
                await eb._connect_to_nats_async()
                await eb._publish_event({"type": "test.pub", "id": "pub-1"})
                mock_js.publish.assert_called_once()
        finally:
            eb.close()

    @pytest.mark.asyncio
    async def test_publish_event_failure(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "test-token")
        mock_nats = MagicMock()
        mock_nats.connect = AsyncMock()
        mock_js = AsyncMock()
        mock_nats.jetstream = MagicMock(return_value=mock_js)
        mock_js.stream_info = AsyncMock()
        mock_js.publish = AsyncMock(side_effect=Exception("publish failed"))

        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            with patch("cerebellum.event_bus.NATS", return_value=mock_nats):
                eb._nats_ready = False
                eb._nc = None
                eb._js = None
                await eb._connect_to_nats_async()
                with pytest.raises(Exception, match="publish failed"):
                    await eb._publish_event({"type": "test.fail", "id": "fail-1"})
        finally:
            eb.close()


class TestEventBusWaitForInflight:
    def test_wait_for_inflight_empty(self, emitter):
        result = emitter._wait_for_inflight_publishes(timeout_seconds=0.1)
        assert result == 0


class TestEventBusSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_success(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "test-token")
        mock_nats = MagicMock()
        mock_nats.connect = AsyncMock()
        mock_js = AsyncMock()
        mock_nats.jetstream = MagicMock(return_value=mock_js)
        mock_js.stream_info = AsyncMock()
        mock_nats.subscribe = AsyncMock()

        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            with patch("cerebellum.event_bus.NATS", return_value=mock_nats):
                eb._nats_ready = False
                eb._nc = None
                eb._js = None
                await eb._connect_to_nats_async()
                await eb._subscribe(lambda e: None)
                mock_nats.subscribe.assert_called_once()
        finally:
            eb.close()

    @pytest.mark.asyncio
    async def test_subscribe_failure(self, config_path, monkeypatch):
        monkeypatch.setenv("CEREBELLUM_NATS_TOKEN", "test-token")
        mock_nats = MagicMock()
        mock_nats.connect = AsyncMock()
        mock_js = AsyncMock()
        mock_nats.jetstream = MagicMock(return_value=mock_js)
        mock_js.stream_info = AsyncMock()
        mock_nats.subscribe = AsyncMock(side_effect=Exception("subscribe failed"))

        from cerebellum.event_bus import EventBus
        eb = EventBus(config_path)
        try:
            with patch("cerebellum.event_bus.NATS", return_value=mock_nats):
                eb._nats_ready = False
                eb._nc = None
                eb._js = None
                await eb._connect_to_nats_async()
                with pytest.raises(Exception, match="subscribe failed"):
                    await eb._subscribe(lambda e: None)
        finally:
            eb.close()


class TestEventBusRowToEvent:
    def test_row_to_event(self, emitter):
        class FakeRow:
            def __getitem__(self, key):
                data = {
                    "id": "r1", "timestamp": "2025-01-01", "type": "test",
                    "payload": "{}", "actor": "system", "context": "{}",
                }
                return data[key]
        event = emitter._row_to_event(FakeRow())
        assert event["id"] == "r1"
        assert event["type"] == "test"


class TestEventBusWriteFailure:
    def test_write_event_persistence_failure(self, emitter):
        with patch.object(emitter, "_sqlite", wraps=emitter._sqlite) as mock_db:
            mock_db.execute.side_effect = sqlite3.OperationalError("db locked")
            with pytest.raises(sqlite3.OperationalError):
                emitter._write_event({
                    "id": "fail-we", "timestamp": "2025-01-01", "type": "test",
                    "payload": {}, "actor": "system", "context": {},
                })
