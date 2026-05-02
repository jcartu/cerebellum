from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cerebellum.http_safe import _safe_opener

logger = logging.getLogger(__name__)


@dataclass
class GroundingResult:
    """Result of grounding verification."""

    proposal_id: str
    verified: bool
    reason: str
    evidence_event_ids: list[str]
    missing_event_ids: list[str]
    causal_argument: str


class GroundingVerifier:
    """Verifies that proposals are grounded in actual events."""

    DEFAULT_VERIFIER_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_VERIFIER_MODEL = "openai/gpt-4o-mini"

    def __init__(self, config_path: str):
        """Initialize the grounding verifier.

        Args:
            config_path: Path to the runtime config JSON.

        Returns:
            None.
        """
        self.config_path = Path(config_path).expanduser()
        self.config = self._load_config()
        grounding_cfg = self.config.get("grounding", {})
        self.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.verifier_url = str(
            grounding_cfg.get("verifier_url", self.DEFAULT_VERIFIER_URL)
        ).strip() or self.DEFAULT_VERIFIER_URL
        self.verifier_model = str(
            grounding_cfg.get("verifier_model", self.DEFAULT_VERIFIER_MODEL)
        ).strip() or self.DEFAULT_VERIFIER_MODEL
        self.app_name = str(self.config.get("app_name", "CEREBELLUM")).strip() or "CEREBELLUM"
        self.site_url = str(self.config.get("site_url", "")).strip() or "https://localhost/cerebellum"

    def verify_evidence_exists(
        self, proposal: dict[str, Any], context_event_ids: set[str]
    ) -> GroundingResult:
        """Verify that cited evidence events exist in the context window.

        Args:
            proposal: Proposal payload to verify.
            context_event_ids: Event IDs available in the current context window.

        Returns:
            Deterministic grounding result for the evidence existence check.
        """
        proposal_id = self._proposal_id(proposal)
        evidence_event_ids = self._coerce_string_list(proposal.get("evidence_event_ids"))
        causal_argument = str(proposal.get("causal_argument") or "")

        if not evidence_event_ids:
            return GroundingResult(
                proposal_id=proposal_id,
                verified=False,
                reason="Proposal cites no evidence events.",
                evidence_event_ids=[],
                missing_event_ids=[],
                causal_argument=causal_argument,
            )

        missing_event_ids = sorted(
            event_id for event_id in evidence_event_ids if event_id not in context_event_ids
        )
        if missing_event_ids:
            return GroundingResult(
                proposal_id=proposal_id,
                verified=False,
                reason="Proposal cites event IDs that are missing from the current context.",
                evidence_event_ids=evidence_event_ids,
                missing_event_ids=missing_event_ids,
                causal_argument=causal_argument,
            )

        return GroundingResult(
            proposal_id=proposal_id,
            verified=True,
            reason="All cited evidence event IDs exist in the current context.",
            evidence_event_ids=evidence_event_ids,
            missing_event_ids=[],
            causal_argument=causal_argument,
        )

    def verify_causal_argument(
        self, proposal: dict[str, Any], events: list[dict[str, Any]]
    ) -> GroundingResult:
        """Verify the proposal's causal argument with a cheap LLM.

        Args:
            proposal: Proposal payload to verify.
            events: Event payloads available as context.

        Returns:
            Grounding result from the LLM verdict.

        Raises:
            RuntimeError: If the OpenRouter request or response parsing fails.
        """
        proposal_id = self._proposal_id(proposal)
        evidence_event_ids = self._coerce_string_list(proposal.get("evidence_event_ids"))
        causal_argument = str(proposal.get("causal_argument") or "")
        prompt = self._build_causal_prompt(proposal, events)
        verdict = self._call_llm(prompt)
        verified = bool(verdict.get("verified", False))
        reason = str(verdict.get("reason") or "LLM verifier returned no reason.")
        return GroundingResult(
            proposal_id=proposal_id,
            verified=verified,
            reason=reason,
            evidence_event_ids=evidence_event_ids,
            missing_event_ids=[],
            causal_argument=causal_argument,
        )

    def verify(
        self, proposal: dict[str, Any], context_event_ids: set[str], events: list[dict[str, Any]]
    ) -> GroundingResult:
        """Run the full grounding verification pipeline.

        Args:
            proposal: Proposal payload to verify.
            context_event_ids: Event IDs available in the current context window.
            events: Event payloads available as context.

        Returns:
            Combined grounding result across deterministic and LLM-backed checks.
        """
        evidence_result = self.verify_evidence_exists(proposal, context_event_ids)
        if not evidence_result.verified:
            logger.info(
                "Skipping causal grounding check for proposal %s because evidence verification failed",
                evidence_result.proposal_id,
            )
            return evidence_result
        return self.verify_causal_argument(proposal, events)

    def _load_config(self) -> dict[str, Any]:
        """Load verifier configuration from disk.

        Args:
            None.

        Returns:
            Parsed JSON config, or an empty dict on failure.
        """
        try:
            if not self.config_path.exists():
                logger.warning(
                    "Grounding verifier config not found at %s; using defaults",
                    self.config_path,
                )
                return {}
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load grounding config %s: %s", self.config_path, exc)
            return {}

    def _build_causal_prompt(self, proposal: dict[str, Any], events: list[dict[str, Any]]) -> str:
        """Build the prompt for causal grounding verification.

        Args:
            proposal: Proposal under review.
            events: Context events supplied to the verifier.

        Returns:
            Prompt text for the verifier model.
        """
        payload = {
            "proposal": {
                "id": self._proposal_id(proposal),
                "title": proposal.get("title"),
                "summary": proposal.get("summary") or proposal.get("description"),
                "action": proposal.get("action"),
                "evidence_event_ids": self._coerce_string_list(proposal.get("evidence_event_ids")),
                "causal_argument": str(proposal.get("causal_argument") or ""),
            },
            "events": events,
        }
        return (
            "Given these events, does the causal argument support the proposed action? "
            'Reply with JSON {"verified": bool, "reason": string}.\n\n'
            f"Context JSON:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    def _call_llm(self, prompt: str) -> dict[str, Any]:
        """Call OpenRouter for causal grounding verification.

        Args:
            prompt: Prompt text for the verifier model.

        Returns:
            Parsed JSON verifier verdict.

        Raises:
            RuntimeError: If the request fails, the response is malformed, or the
                parsed verdict is invalid.
        """
        if not self.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")

        payload = {
            "model": self.verifier_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
        }
        request = urllib.request.Request(
            self.verifier_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "HTTP-Referer": self.site_url,
                "X-Title": self.app_name,
            },
            method="POST",
        )

        try:
            with _safe_opener.open(request, timeout=60) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

        content = self._read_nested_key(response_payload, "choices.0.message.content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenRouter response missing verifier content")

        verdict = self._extract_json_object(content)
        verified = verdict.get("verified")
        reason = verdict.get("reason")
        if not isinstance(verified, bool):
            raise RuntimeError("OpenRouter verifier response missing boolean 'verified'")
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeError("OpenRouter verifier response missing string 'reason'")
        return {"verified": verified, "reason": reason.strip()}

    def _read_nested_key(self, data: Any, key_path: str) -> Any:
        """Read a dotted path from nested dict/list data.

        Args:
            data: Nested data structure.
            key_path: Dotted path with numeric list indexes.

        Returns:
            The nested value, or None if any segment is missing.
        """
        current = data
        for key in key_path.split("."):
            if isinstance(current, dict):
                current = current.get(key)
            elif isinstance(current, list):
                try:
                    current = current[int(key)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return current

    def _extract_json_object(self, text: str) -> dict[str, Any]:
        """Extract a JSON object from raw model text.

        Args:
            text: Model response text.

        Returns:
            Parsed JSON object.

        Raises:
            RuntimeError: If no valid JSON object can be extracted.
        """
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise RuntimeError("OpenRouter verifier response did not contain JSON") from None
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"OpenRouter verifier JSON parsing failed: {exc}") from exc

        if not isinstance(parsed, dict):
            raise RuntimeError("OpenRouter verifier response must be a JSON object")
        return parsed

    def _proposal_id(self, proposal: dict[str, Any]) -> str:
        """Extract a stable proposal identifier.

        Args:
            proposal: Proposal payload.

        Returns:
            Proposal identifier string, or an empty string if absent.
        """
        return str(proposal.get("id") or "")

    def _coerce_string_list(self, value: Any) -> list[str]:
        """Normalize a list-like value into a list of strings.

        Args:
            value: Candidate list value.

        Returns:
            Stringified list elements, or an empty list for non-lists.
        """
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]
