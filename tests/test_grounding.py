from __future__ import annotations

import json

import pytest

from cerebellum.grounding import GroundingResult, GroundingVerifier


def _write_config(tmp_path, **grounding_overrides):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "app_name": "Test Cerebellum",
                "site_url": "https://example.test/cerebellum",
                "grounding": {
                    "verifier_url": grounding_overrides.get(
                        "verifier_url", "https://openrouter.ai/api/v1/chat/completions"
                    ),
                    "verifier_model": grounding_overrides.get(
                        "verifier_model", "openai/gpt-4o-mini"
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_verify_evidence_exists_rejects_empty_evidence(tmp_path):
    verifier = GroundingVerifier(str(_write_config(tmp_path)))

    result = verifier.verify_evidence_exists({"id": "proposal-1", "causal_argument": "Because."}, {"evt-1"})

    assert result == GroundingResult(
        proposal_id="proposal-1",
        verified=False,
        reason="Proposal cites no evidence events.",
        evidence_event_ids=[],
        missing_event_ids=[],
        causal_argument="Because.",
    )


def test_verify_evidence_exists_rejects_missing_context_events(tmp_path):
    verifier = GroundingVerifier(str(_write_config(tmp_path)))

    result = verifier.verify_evidence_exists(
        {
            "id": "proposal-2",
            "evidence_event_ids": ["evt-1", "evt-2"],
            "causal_argument": "These events justify the action.",
        },
        {"evt-1"},
    )

    assert result.proposal_id == "proposal-2"
    assert result.verified is False
    assert result.evidence_event_ids == ["evt-1", "evt-2"]
    assert result.missing_event_ids == ["evt-2"]
    assert "missing from the current context" in result.reason


def test_verify_evidence_exists_accepts_valid_evidence(tmp_path):
    verifier = GroundingVerifier(str(_write_config(tmp_path)))

    result = verifier.verify_evidence_exists(
        {
            "id": "proposal-3",
            "evidence_event_ids": ["evt-1", "evt-2"],
            "causal_argument": "Pattern is consistent.",
        },
        {"evt-1", "evt-2", "evt-3"},
    )

    assert result.proposal_id == "proposal-3"
    assert result.verified is True
    assert result.evidence_event_ids == ["evt-1", "evt-2"]
    assert result.missing_event_ids == []
    assert result.causal_argument == "Pattern is consistent."


def test_verify_short_circuits_when_evidence_fails(tmp_path, monkeypatch):
    verifier = GroundingVerifier(str(_write_config(tmp_path)))
    called = False

    def _unexpected_call(proposal, events):
        nonlocal called
        called = True
        raise AssertionError("verify_causal_argument should not be called")

    monkeypatch.setattr(verifier, "verify_causal_argument", _unexpected_call)

    result = verifier.verify(
        {"id": "proposal-4", "evidence_event_ids": [], "causal_argument": "No evidence."},
        {"evt-1"},
        [{"id": "evt-1"}],
    )

    assert result.verified is False
    assert result.reason == "Proposal cites no evidence events."
    assert called is False


def test_verify_causal_argument_uses_mocked_llm(tmp_path, monkeypatch):
    verifier = GroundingVerifier(str(_write_config(tmp_path, verifier_model="openai/gpt-4o-mini")))

    captured_prompt = ""

    def _mock_call_llm(prompt: str):
        nonlocal captured_prompt
        captured_prompt = prompt
        return {"verified": True, "reason": "Events support the action."}

    monkeypatch.setattr(verifier, "_call_llm", _mock_call_llm)

    proposal = {
        "id": "proposal-5",
        "title": "Follow up on failed deploy",
        "action": "Open incident",
        "evidence_event_ids": ["evt-9"],
        "causal_argument": "A failed deploy followed repeated retries.",
    }
    events = [{"id": "evt-9", "type": "deploy.failed", "payload": {"service": "api"}}]

    result = verifier.verify_causal_argument(proposal, events)

    assert result == GroundingResult(
        proposal_id="proposal-5",
        verified=True,
        reason="Events support the action.",
        evidence_event_ids=["evt-9"],
        missing_event_ids=[],
        causal_argument="A failed deploy followed repeated retries.",
    )
    assert "Follow up on failed deploy" in captured_prompt
    assert "deploy.failed" in captured_prompt


def test_grounding_result_dataclass_structure():
    result = GroundingResult(
        proposal_id="proposal-6",
        verified=False,
        reason="Insufficient support.",
        evidence_event_ids=["evt-1"],
        missing_event_ids=["evt-2"],
        causal_argument="Weak causal chain.",
    )

    assert result.proposal_id == "proposal-6"
    assert result.verified is False
    assert result.reason == "Insufficient support."
    assert result.evidence_event_ids == ["evt-1"]
    assert result.missing_event_ids == ["evt-2"]
    assert result.causal_argument == "Weak causal chain."


def test_missing_config_uses_defaults(tmp_path):
    verifier = GroundingVerifier(str(tmp_path / "missing-config.json"))

    assert verifier.config == {}
    assert verifier.verifier_url == "https://openrouter.ai/api/v1/chat/completions"
    assert verifier.verifier_model == "openai/gpt-4o-mini"
    assert verifier.app_name == "CEREBELLUM"
    assert verifier.site_url == "https://localhost/cerebellum"


def test_call_llm_parses_json_response_with_safe_opener(tmp_path, monkeypatch):
    verifier = GroundingVerifier(str(_write_config(tmp_path)))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier.openrouter_api_key = "test-key"

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": 'Verifier says: {"verified": true, "reason": "Supported."}'
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    class _FakeOpener:
        def open(self, request, timeout):
            assert request.full_url == verifier.verifier_url
            assert timeout == 60
            assert request.headers["Authorization"] == "Bearer test-key"
            return _FakeResponse()

    monkeypatch.setattr("cerebellum.grounding.safe_post_bytes", lambda *a, **k: json.dumps({"choices": [{"message": {"content": 'Verifier says: {"verified": true, "reason": "Supported."}'}}]}).encode("utf-8"))

    result = verifier._call_llm("check grounding")

    assert result == {"verified": True, "reason": "Supported."}


def test_call_llm_raises_for_missing_verifier_content(tmp_path, monkeypatch):
    verifier = GroundingVerifier(str(_write_config(tmp_path)))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    verifier.openrouter_api_key = "test-key"

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "   "}}]}).encode("utf-8")

    class _FakeOpener:
        def open(self, request, timeout):
            return _FakeResponse()

    monkeypatch.setattr("cerebellum.grounding.safe_post_bytes", lambda *a, **k: json.dumps({"choices": [{"message": {"content": "   "}}]}).encode("utf-8"))

    with pytest.raises(RuntimeError, match="missing verifier content"):
        verifier._call_llm("check grounding")


def test_extract_json_object_rejects_non_object_payload(tmp_path):
    verifier = GroundingVerifier(str(_write_config(tmp_path)))

    with pytest.raises(RuntimeError, match="must be a JSON object"):
        verifier._extract_json_object("[]")


def test_read_nested_key_returns_none_for_missing_path(tmp_path):
    verifier = GroundingVerifier(str(_write_config(tmp_path)))

    assert verifier._read_nested_key({"choices": []}, "choices.0.message.content") is None
