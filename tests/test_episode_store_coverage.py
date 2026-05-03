"""Episode store coverage tests — boost coverage from 69% to 80%."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from cerebellum.episode_store import EpisodeStore, LLMResponse


@pytest.fixture(scope="module")
def episode_store(tmp_path_factory):
    """Create a single EpisodeStore shared across all tests in this module."""
    tmp_path = tmp_path_factory.mktemp("episode_store")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "hippocampus": {
                "graph_path": str(tmp_path / "graph"),
                "openrouter_url": "https://openrouter.ai/api/v1/chat/completions",
                "openrouter_model": "openai/gpt-4o-mini",
            }
        })
    )
    store = EpisodeStore(str(config_path))
    yield store
    store.close()


class TestCallLLM:
    """Test _call_llm LLM Cypher generation paths (lines 721-750)."""

    def test_call_llm_success(self, episode_store, monkeypatch):
        episode_store.openrouter_api_key = "test-key"
        mock_response = json.dumps({
            "choices": [{"message": {"content": "MATCH (n) RETURN n"}}]
        }).encode("utf-8")
        monkeypatch.setattr(
            "cerebellum.episode_store.safe_post_bytes",
            lambda *args, **kwargs: mock_response
        )
        result = episode_store._call_llm("Generate Cypher query")
        assert isinstance(result, LLMResponse)
        assert result.text == "MATCH (n) RETURN n"

    def test_call_llm_missing_api_key(self, episode_store):
        episode_store.openrouter_api_key = None
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
            episode_store._call_llm("test prompt")

    def test_call_llm_timeout_error(self, episode_store, monkeypatch):
        episode_store.openrouter_api_key = "test-key"
        monkeypatch.setattr(
            "cerebellum.episode_store.safe_post_bytes",
            lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timeout"))
        )
        with pytest.raises(RuntimeError, match="OpenRouter request failed"):
            episode_store._call_llm("test prompt")

    def test_call_llm_json_decode_error(self, episode_store, monkeypatch):
        episode_store.openrouter_api_key = "test-key"
        monkeypatch.setattr(
            "cerebellum.episode_store.safe_post_bytes",
            lambda *args, **kwargs: b"not json"
        )
        with pytest.raises(RuntimeError, match="OpenRouter request failed"):
            episode_store._call_llm("test prompt")

    def test_call_llm_missing_text_in_response(self, episode_store, monkeypatch):
        episode_store.openrouter_api_key = "test-key"
        mock_response = json.dumps({
            "choices": [{"message": {"content": "   "}}]
        }).encode("utf-8")
        monkeypatch.setattr(
            "cerebellum.episode_store.safe_post_bytes",
            lambda *args, **kwargs: mock_response
        )
        with pytest.raises(RuntimeError, match="OpenRouter response missing text"):
            episode_store._call_llm("test prompt")

    def test_call_llm_response_parsing_failure(self, episode_store, monkeypatch):
        episode_store.openrouter_api_key = "test-key"
        mock_response = json.dumps({"invalid": "structure"}).encode("utf-8")
        monkeypatch.setattr(
            "cerebellum.episode_store.safe_post_bytes",
            lambda *args, **kwargs: mock_response
        )
        with pytest.raises(RuntimeError, match="OpenRouter response missing text"):
            episode_store._call_llm("test prompt")


class TestReadNestedKey:
    """Test _read_nested_key paths (lines 753-765)."""

    def test_read_nested_key_dict_path(self, episode_store):
        data = {"a": {"b": {"c": "value"}}}
        assert episode_store._read_nested_key(data, "a.b.c") == "value"

    def test_read_nested_key_list_index(self, episode_store):
        data = {"choices": [{"message": {"content": "test"}}]}
        assert episode_store._read_nested_key(data, "choices.0.message.content") == "test"

    def test_read_nested_key_missing_key(self, episode_store):
        data = {"a": {"b": "value"}}
        assert episode_store._read_nested_key(data, "a.c") is None

    def test_read_nested_key_invalid_index(self, episode_store):
        data = {"items": [1, 2, 3]}
        assert episode_store._read_nested_key(data, "items.5") is None

    def test_read_nested_key_non_numeric_index(self, episode_store):
        data = {"items": [1, 2, 3]}
        assert episode_store._read_nested_key(data, "items.abc") is None

    def test_read_nested_key_string_value(self, episode_store):
        data = {"a": "string_value"}
        assert episode_store._read_nested_key(data, "a.b") is None


class TestExtractJsonObject:
    """Test _extract_json_object paths (lines 769-782)."""

    def test_extract_json_object_direct(self, episode_store):
        result = episode_store._extract_json_object('{"key": "value"}')
        assert result == {"key": "value"}

    def test_extract_json_object_with_surrounding_text(self, episode_store):
        text = 'Here\'s the result: {"key": "value"} and more text'
        result = episode_store._extract_json_object(text)
        assert result == {"key": "value"}

    def test_extract_json_object_invalid_json(self, episode_store):
        result = episode_store._extract_json_object("not json at all")
        assert result == {}

    def test_extract_json_object_partial_braces(self, episode_store):
        result = episode_store._extract_json_object("text with { but no closing")
        assert result == {}

    def test_extract_json_object_nested(self, episode_store):
        text = '{"outer": {"inner": [1, 2, 3]}}'
        result = episode_store._extract_json_object(text)
        assert result == {"outer": {"inner": [1, 2, 3]}}


class TestIsSafeReadQuery:
    """Test _is_safe_read_query edge cases (lines 844-862)."""

    def test_is_safe_read_query_empty(self, episode_store):
        assert episode_store._is_safe_read_query("") is False
        assert episode_store._is_safe_read_query("   ") is False

    def test_is_safe_read_query_with_semicolon(self, episode_store):
        assert episode_store._is_safe_read_query("MATCH (n); DELETE n") is False

    def test_is_safe_read_query_safe_match(self, episode_store):
        assert episode_store._is_safe_read_query("MATCH (n) RETURN n") is True

    def test_is_safe_read_query_create_blocked(self, episode_store):
        assert episode_store._is_safe_read_query("CREATE (n:Node)") is False

    def test_is_safe_read_query_delete_blocked(self, episode_store):
        assert episode_store._is_safe_read_query("MATCH (n) DELETE n") is False

    def test_is_safe_read_query_merge_blocked(self, episode_store):
        assert episode_store._is_safe_read_query("MERGE (n:Node)") is False

    def test_is_safe_read_query_set_blocked(self, episode_store):
        assert episode_store._is_safe_read_query("MATCH (n) SET n.prop = 1") is False

    def test_is_safe_read_query_remove_blocked(self, episode_store):
        assert episode_store._is_safe_read_query("MATCH (n) REMOVE n.label") is False

    def test_is_safe_read_query_with_string_literals(self, episode_store):
        query = 'MATCH (n {name: "CREATE"}) RETURN n'
        assert episode_store._is_safe_read_query(query) is True


class TestGenerateQueryFromNL:
    """Test _generate_query_from_nl paths (lines 685-715)."""

    def test_generate_query_success(self, episode_store, monkeypatch):
        episode_store.openrouter_api_key = "test-key"
        mock_response = LLMResponse(
            text='{"query": "Find all nodes"}',
            raw={"choices": [{"message": {"content": '{"query": "Find all nodes"}'}}]}
        )
        monkeypatch.setattr(episode_store, "_call_llm", MagicMock(return_value=mock_response))
        result = episode_store._generate_query_from_nl("test context")
        assert result == "Find all nodes"

    def test_generate_query_no_api_key(self, episode_store):
        episode_store.openrouter_api_key = None
        result = episode_store._generate_query_from_nl("test context")
        assert result is None

    def test_generate_query_llm_failure(self, episode_store, monkeypatch):
        episode_store.openrouter_api_key = "test-key"
        monkeypatch.setattr(
            episode_store, "_call_llm", MagicMock(side_effect=RuntimeError("LLM failed"))
        )
        result = episode_store._generate_query_from_nl("test context")
        assert result is None

    def test_generate_query_empty_result(self, episode_store, monkeypatch):
        episode_store.openrouter_api_key = "test-key"
        mock_response = LLMResponse(text="{}", raw={})
        monkeypatch.setattr(episode_store, "_call_llm", MagicMock(return_value=mock_response))
        result = episode_store._generate_query_from_nl("test context")
        assert result is None


class TestStripQueryComments:
    """Test _strip_query_comments edge cases."""

    def test_strip_query_comments_removes_line_comments(self, episode_store):
        result = episode_store._strip_query_comments("MATCH (n) -- comment\nRETURN n")
        assert "-- comment" not in result

    def test_strip_query_comments_removes_block_comments(self, episode_store):
        result = episode_store._strip_query_comments("MATCH (n) /* comment */ RETURN n")
        assert "/* comment */" not in result

    def test_strip_query_comments_empty_string(self, episode_store):
        result = episode_store._strip_query_comments("")
        assert result == ""


class TestFetchAllReadOnly:
    """Test _fetch_all_read_only paths (lines 548-567)."""

    def test_fetch_all_read_only_safe_query(self, episode_store):
        result = episode_store._fetch_all_read_only("MATCH (n:Episode) RETURN n.id LIMIT 1")
        assert isinstance(result, list)

    def test_fetch_all_read_only_unsafe_query_raises(self, episode_store):
        with pytest.raises(ValueError, match="Refusing to execute non-read-only query"):
            episode_store._fetch_all_read_only("CREATE (n:Node)")

    def test_fetch_all_read_only_with_comments(self, episode_store):
        result = episode_store._fetch_all_read_only("MATCH (n:Episode) -- comment\nRETURN n.id LIMIT 1")
        assert isinstance(result, list)


class TestQueryMethod:
    """Test query() method paths (lines 321-334)."""

    def test_query_fallback_when_no_llm(self, episode_store):
        episode_store.openrouter_api_key = None
        result = episode_store.query("test query")
        assert isinstance(result, dict)

    def test_query_rejects_unsafe_llm_query(self, episode_store, monkeypatch):
        episode_store.openrouter_api_key = "test-key"
        mock_response = LLMResponse(
            text='{"query": "CREATE (n:Node)"}',
            raw={"choices": [{"message": {"content": '{"query": "CREATE (n:Node)"}'}}]}
        )
        monkeypatch.setattr(episode_store, "_call_llm", MagicMock(return_value=mock_response))
        result = episode_store.query("test query")
        # Should fall back to heuristic when LLM generates unsafe query
        assert isinstance(result, dict)


class TestNormalizeEvent:
    """Test _normalize_event edge cases (lines 590-595)."""

    def test_normalize_event_string_payload(self, episode_store):
        event = {"id": "test-1", "type": "test.event", "timestamp": "2026-01-01T00:00:00Z", "payload": '{"key": "value"}'}
        result = episode_store._normalize_event(event)
        assert result["payload"] == {"key": "value"}

    def test_normalize_event_invalid_string_payload(self, episode_store):
        event = {"id": "test-2", "type": "test.event", "timestamp": "2026-01-01T00:00:00Z", "payload": "not json"}
        result = episode_store._normalize_event(event)
        assert result["payload"] == {"raw": "not json"}

    def test_normalize_event_non_dict_payload(self, episode_store):
        event = {"id": "test-3", "type": "test.event", "timestamp": "2026-01-01T00:00:00Z", "payload": 42}
        result = episode_store._normalize_event(event)
        assert result["payload"] == {"raw": 42}

    def test_normalize_event_no_id(self, episode_store):
        event = {"type": "test.event", "timestamp": "2026-01-01T00:00:00Z", "payload": {}}
        result = episode_store._normalize_event(event)
        assert result["id"].startswith("event:")

    def test_normalize_event_no_type(self, episode_store):
        event = {"id": "test-4", "timestamp": "2026-01-01T00:00:00Z", "payload": {}}
        result = episode_store._normalize_event(event)
        assert result["type"] == "unknown"


class TestGetRecentEpisodes:
    """Test get_recent_episodes error path (lines 353-355)."""

    def test_get_recent_episodes_returns_list(self, episode_store):
        result = episode_store.get_recent_episodes(limit=5)
        assert isinstance(result, list)


class TestHeuristicQuery:
    """Test _heuristic_query error path (lines 681-683)."""

    def test_heuristic_query_unknown(self, episode_store):
        result = episode_store._heuristic_query("totally unknown query xyz")
        assert "ok" in result or "mode" in result


class TestQueryLLMSuccess:
    """Test query() method LLM success path (lines 325-334)."""

    def test_query_llm_success(self, episode_store, monkeypatch):
        episode_store.openrouter_api_key = "test-key"
        mock_response = LLMResponse(
            text='{"query": "MATCH (n:Episode) RETURN n.id LIMIT 1"}',
            raw={"choices": [{"message": {"content": '{"query": "MATCH (n:Episode) RETURN n.id LIMIT 1"}'}}]}
        )
        monkeypatch.setattr(episode_store, "_call_llm", MagicMock(return_value=mock_response))
        result = episode_store.query("show episodes")
        assert result.get("mode") == "llm"
        assert "rows" in result
