"""Hypothesis generation and lifecycle management for CEREBELLUM."""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - handled gracefully at runtime
    OpenAI = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent
EMPTY_USAGE: dict[str, Any] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "model": "",
}


@dataclass
class Hypothesis:
    id: str
    timestamp: str
    title: str
    description: str
    confidence: float
    utility: float
    generation_cost_usd: float
    estimated_execution_cost_usd: float | None
    reversibility: str
    plan: list[str]
    tools_required: list[str]
    context_summary: str
    state: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Proposer:
    """Hypothesis generation engine."""

    DEFAULT_MODELS = ["openai/gpt-4o", "anthropic/claude-opus-4-7"]
    VALID_STATES = {
        "proposed",
        "staged",
        "pending_approval",
        "snoozed",
        "executed",
        "completed",
        "rejected",
        "expired",
    }
    VALID_REVERSIBILITY = {"full", "partial", "none"}
    MAX_LLM_RESPONSE_BYTES = 8 * 1024 * 1024
    EXPECTED_HYPOTHESIS_KEYS = {
        "title",
        "description",
        "confidence",
        "utility",
        "cost",
        "reversibility",
        "plan",
        "tools_required",
        "context_summary",
        "metadata",
    }

    def __init__(self, config_path: str, emitter=None, hippocampus=None):
        self.config_path = Path(config_path).expanduser()
        self.base_dir = self.config_path.parent
        self.db_path = self.base_dir / "hypotheses.db"
        self.emitter = emitter
        self.hippocampus = hippocampus
        self.api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        self.config = self._load_config()
        self.model_candidates = self.config.get("models") or self.DEFAULT_MODELS
        self.openrouter_base_url = self.config.get(
            "openrouter_base_url", "https://openrouter.ai/api/v1"
        )
        self.generation_interval_minutes = int(self.config.get("generation_interval_minutes", 5))
        self.app_name = self.config.get("app_name", "CEREBELLUM")
        self.site_url = self.config.get("site_url")
        self._db_lock = threading.RLock()
        self._sqlite: sqlite3.Connection | None = None
        self._checkpoint_interval_seconds = 300
        self._checkpoint_stop = threading.Event()
        self._checkpoint_thread: threading.Thread | None = None
        self._client = self._build_client()
        self._init_db()

    def _load_config(self) -> dict[str, Any]:
        try:
            if not self.config_path.exists():
                logger.warning("Proposer config not found at %s; using defaults", self.config_path)
                return {}
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load proposer config %s: %s", self.config_path, exc)
            return {}

    def _build_client(self):
        if not self.api_key:
            logger.warning("OPENROUTER_API_KEY is not set; LLM generation disabled")
            return None
        if OpenAI is None:
            logger.error("openai package is not installed; LLM generation disabled")
            return None
        try:
            headers = {"HTTP-Referer": self.site_url or "https://localhost/cerebellum", "X-Title": self.app_name}
            return OpenAI(api_key=self.api_key, base_url=self.openrouter_base_url, default_headers=headers)
        except Exception as exc:
            logger.error("Failed to initialize OpenRouter client: %s", self._redact_openrouter_error(exc))
            return None

    def _redact_openrouter_error(self, exc: Exception) -> str:
        message = str(exc)
        if not self.api_key:
            return message
        return message.replace(self.api_key, "sk-or-v1-***")

    def _get_connection(self) -> sqlite3.Connection:
        if self._sqlite is None:
            raise RuntimeError("Hypotheses database is not initialized")
        return self._sqlite

    def _init_db(self):
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with self._db_lock:
                self._sqlite = sqlite3.connect(self.db_path, check_same_thread=False)
                self._sqlite.row_factory = sqlite3.Row
                self._sqlite.execute("PRAGMA journal_mode=WAL")
                self._sqlite.execute("PRAGMA synchronous=NORMAL")
                self._sqlite.execute("PRAGMA wal_autocheckpoint=1000")
                conn = self._get_connection()
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS hypotheses (
                      id TEXT PRIMARY KEY,
                      timestamp TEXT,
                      title TEXT,
                      description TEXT,
                      confidence REAL,
                      utility REAL,
                      generation_cost_usd REAL,
                      estimated_execution_cost_usd REAL,
                      reversibility TEXT,
                      plan TEXT,
                      tools_required TEXT,
                      context_summary TEXT,
                      state TEXT DEFAULT 'proposed',
                      metadata TEXT,
                      created_at TEXT DEFAULT (datetime('now')),
                      updated_at TEXT DEFAULT (datetime('now'))
                    )
                    """
                )
                self._ensure_hypothesis_schema(conn)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_hypotheses_state_timestamp ON hypotheses(state, timestamp DESC)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_hypotheses_created_at ON hypotheses(created_at DESC)"
                )
                conn.commit()
            self._start_checkpoint_worker()
        except Exception as exc:
            logger.error("Failed to initialize hypotheses DB at %s: %s", self.db_path, exc)

    def generate_hypotheses(self) -> list[Hypothesis]:
        """Main method: generate hypotheses from current state."""
        try:
            episodes = self._get_recent_episodes(hours=2)
            events = self._get_recent_events(minutes=30)
            existing = self.get_active_hypotheses(state="proposed", limit=25)
            prompt = self._build_prompt(episodes=episodes, events=events, existing=existing)
            raw_hypotheses, usage = self._call_llm(prompt)
            if not raw_hypotheses:
                return []

            stored: list[Hypothesis] = []
            for item in raw_hypotheses:
                hypothesis = self._coerce_hypothesis(item, episodes=episodes, events=events, usage=usage)
                if hypothesis is None:
                    continue
                if self._is_duplicate(hypothesis, existing + [entry.to_dict() for entry in stored]):
                    logger.info("Skipping duplicate hypothesis candidate: %s", hypothesis.title)
                    continue
                if self._store_hypothesis(hypothesis):
                    stored.append(hypothesis)
                    self._emit_event(
                        "cerebellum.hypothesis",
                        {
                            "id": hypothesis.id,
                            "state": hypothesis.state,
                            "title": hypothesis.title,
                            "confidence": hypothesis.confidence,
                            "utility": hypothesis.utility,
                        },
                    )
            return stored
        except Exception as exc:
            logger.error("Failed to generate hypotheses: %s", exc, exc_info=True)
            return []

    def _build_prompt(self, episodes: list, events: list, existing: list) -> str:
        """Build the hypothesis generation prompt."""
        try:
            existing_titles = [str(item.get("title", "")).strip() for item in existing][:10]
            payload = {
                "recent_episodes": episodes[-12:],
                "recent_events": events[-25:],
                "existing_hypotheses": [
                    {
                        "title": item.get("title"),
                        "description": item.get("description"),
                        "state": item.get("state"),
                    }
                    for item in existing[:10]
                ],
                "generation_interval_minutes": self.generation_interval_minutes,
                "current_time": datetime.now(UTC).isoformat(),
            }
            return (
                "You are the proposer for CEREBELLUM, a proactive ops assistant for RASPUTIN. "
                "Analyze recent system behavior and propose a small set of high-value hypotheses about useful next actions, risks, or follow-up work.\n\n"
                "Rules:\n"
                "1. Return ONLY valid JSON. No markdown, no prose, no code fences.\n"
                "2. The JSON must be an array of 0 to 5 objects.\n"
                "3. Every object must include exactly these keys: title, description, confidence, utility, cost, reversibility, plan, tools_required, context_summary, metadata.\n"
                "4. confidence, utility, and cost must be calibrated floats between 0.0 and 1.0. Avoid inflated confidence.\n"
                "5. reversibility must be one of: full, partial, none.\n"
                "6. plan must be a concrete ordered array of short executable steps, not vague intentions.\n"
                "7. tools_required must name real tools, systems, or capabilities needed to execute the plan.\n"
                "8. context_summary must concisely explain the evidence that triggered the hypothesis.\n"
                "9. metadata must be a JSON object with optional supporting details such as observed_patterns, risk_level, or estimated_minutes.\n"
                "10. Only generate SPECIFIC and ACTIONABLE hypotheses with plausible user value in the next hour to day.\n"
                "11. Avoid duplicates or near-duplicates of existing hypotheses, especially these titles: "
                f"{existing_titles or ['none']}.\n"
                "12. Prefer hypotheses that either unblock ongoing work, surface hidden risk, or capitalize on clear opportunities.\n"
                "13. If the context is too weak, return an empty array.\n\n"
                "Context JSON:\n"
                f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
            )
        except Exception as exc:
            logger.error("Failed to build hypothesis prompt: %s", exc)
            return "Return an empty JSON array: []"

    def _call_llm(self, prompt: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Call OpenRouter API for hypothesis generation."""
        if self._client is None:
            return [], dict(EMPTY_USAGE)

        for model in self.model_candidates:
            for attempt in range(3):
                try:
                    response = self._client.chat.completions.create(
                        model=model,
                        temperature=0.3,
                        messages=[
                            {
                                "role": "system",
                                "content": "You produce rigorous, structured hypothesis proposals as strict JSON.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                    )
                    usage = self._extract_usage(response, model=model)
                    content = self._clean_json_content(self._extract_message_content(response))
                    content_size = len(content.encode("utf-8"))
                    if content_size > self.MAX_LLM_RESPONSE_BYTES:
                        logger.error(
                            "Rejecting oversized LLM response for model %s: %d bytes exceeds %d",
                            model,
                            content_size,
                            self.MAX_LLM_RESPONSE_BYTES,
                        )
                        return [], usage
                    hypotheses = self._parse_hypothesis_response(content, model=model)
                    if hypotheses is not None:
                        return hypotheses, usage
                    return [], usage
                except Exception as exc:
                    if attempt >= 2:
                        logger.error("LLM call failed for model %s after retries: %s", model, exc)
                        break
                    delay = 2**attempt
                    logger.warning(
                        "LLM call failed for model %s on attempt %s/3: %s; retrying in %ss",
                        model,
                        attempt + 1,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
        return [], dict(EMPTY_USAGE)

    def _ensure_hypothesis_schema(self, conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(hypotheses)").fetchall()}
        if "generation_cost_usd" not in columns:
            conn.execute("ALTER TABLE hypotheses ADD COLUMN generation_cost_usd REAL")
        if "estimated_execution_cost_usd" not in columns:
            conn.execute("ALTER TABLE hypotheses ADD COLUMN estimated_execution_cost_usd REAL")
        if "cost" in columns:
            conn.execute(
                """
                UPDATE hypotheses
                SET generation_cost_usd = COALESCE(generation_cost_usd, cost, 0.0)
                WHERE generation_cost_usd IS NULL
                """
            )
        else:
            conn.execute(
                "UPDATE hypotheses SET generation_cost_usd = COALESCE(generation_cost_usd, 0.0) WHERE generation_cost_usd IS NULL"
            )

    def _extract_usage(self, response: Any, model: str | None = None) -> dict[str, Any]:
        usage_payload = dict(EMPTY_USAGE)
        usage = getattr(response, "usage", None)
        try:
            prompt_tokens = max(0, int(getattr(usage, "prompt_tokens", 0) or 0))
            completion_tokens = max(0, int(getattr(usage, "completion_tokens", 0) or 0))
            total_tokens = max(
                0,
                int(getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)),
            )
        except (TypeError, ValueError) as exc:
            logger.error("Rejecting malformed LLM usage payload for model %s: %s", model or "", exc)
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "model": str(getattr(response, "model", None) or model or ""),
            }
        actual_model = str(getattr(response, "model", None) or model or "")
        logger.info(
            "LLM usage: model=%s prompt=%s completion=%s total=%s",
            actual_model,
            prompt_tokens,
            completion_tokens,
            total_tokens,
        )
        usage_payload.update(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "model": actual_model,
            }
        )
        return usage_payload

    def _parse_hypothesis_response(self, content: str, model: str) -> list[dict[str, Any]] | None:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.error("Rejecting malformed JSON from model %s: %s", model, exc)
            return None

        hypotheses: Any
        if isinstance(parsed, list):
            hypotheses = parsed
        elif isinstance(parsed, dict):
            hypotheses = parsed.get("hypotheses")
            if hypotheses is None:
                logger.error("Rejecting malformed JSON from model %s: missing 'hypotheses' key", model)
                return None
        else:
            logger.error("Rejecting malformed JSON from model %s: top-level payload must be a list or object", model)
            return None

        if not isinstance(hypotheses, list):
            logger.error("Rejecting malformed JSON from model %s: hypotheses payload must be a list", model)
            return None

        valid_hypotheses: list[dict[str, Any]] = []
        for item in hypotheses:
            if not isinstance(item, dict):
                logger.error("Rejecting non-object hypothesis from model %s: %r", model, item)
                return None
            missing_keys = self.EXPECTED_HYPOTHESIS_KEYS.difference(item)
            if missing_keys:
                logger.error(
                    "Rejecting malformed hypothesis from model %s; missing keys: %s",
                    model,
                    sorted(missing_keys),
                )
                return None
            if not isinstance(item.get("plan"), list):
                logger.error("Rejecting malformed hypothesis from model %s: plan must be a list", model)
                return None
            if not isinstance(item.get("tools_required"), list):
                logger.error("Rejecting malformed hypothesis from model %s: tools_required must be a list", model)
                return None
            if not isinstance(item.get("metadata"), dict):
                logger.error("Rejecting malformed hypothesis from model %s: metadata must be an object", model)
                return None
            valid_hypotheses.append(item)
        return valid_hypotheses

    def _extract_message_content(self, response: Any) -> str:
        try:
            try:
                message = response.choices[0].message
                content = message.content
            except TypeError as exc:
                raise ValueError(f"Unable to extract LLM content: {exc}") from exc
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                fragments: list[str] = []
                for chunk in content:
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        fragments.append(str(chunk.get("text", "")))
                    else:
                        try:
                            text_value = getattr(chunk, "text", None)
                        except TypeError as exc:
                            raise ValueError(f"Unable to extract LLM content: {exc}") from exc
                        if text_value:
                            fragments.append(str(text_value))
                return "".join(fragments)
            return str(content)
        except Exception as exc:
            raise ValueError(f"Unable to extract LLM content: {exc}") from exc

    def _clean_json_content(self, content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        return stripped

    def _start_checkpoint_worker(self) -> None:
        if self._checkpoint_thread is not None and self._checkpoint_thread.is_alive():
            return
        self._checkpoint_thread = threading.Thread(
            target=self._checkpoint_worker,
            name="cerebellum-proposer-wal-checkpoint",
            daemon=True,
        )
        self._checkpoint_thread.start()

    def _checkpoint_worker(self) -> None:
        while not self._checkpoint_stop.wait(self._checkpoint_interval_seconds):
            try:
                with self._db_lock:
                    self._get_connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as exc:
                logger.warning("Proposer WAL checkpoint failed: %s", exc)

    def get_active_hypotheses(self, state: str | None = None, limit: int = 50) -> list[dict]:
        """Query hypotheses by state."""
        try:
            sql = "SELECT * FROM hypotheses"
            params: list[Any] = []
            if state:
                sql += " WHERE state = ?"
                params.append(state)
            sql += " ORDER BY timestamp DESC, created_at DESC LIMIT ?"
            params.append(max(1, limit))
            with self._db_lock:
                conn = self._get_connection()
                rows = conn.execute(sql, params).fetchall()
            return [self._row_to_dict(row) for row in rows]
        except Exception as exc:
            logger.error("Failed to query hypotheses: %s", exc)
            return []

    def get_hypothesis(self, hypothesis_id: str) -> dict[str, Any] | None:
        try:
            with self._db_lock:
                conn = self._get_connection()
                row = conn.execute("SELECT * FROM hypotheses WHERE id = ?", (hypothesis_id,)).fetchone()
            return self._row_to_dict(row) if row else None
        except Exception as exc:
            logger.error("Failed to fetch hypothesis %s: %s", hypothesis_id, exc)
            return None

    def update_hypothesis_state(self, hypothesis_id: str, new_state: str, reason: str = "") -> bool:
        """Update hypothesis state, emit event."""
        try:
            if new_state not in self.VALID_STATES:
                raise ValueError(f"Invalid hypothesis state: {new_state}")
            hypothesis = self.get_hypothesis(hypothesis_id)
            if not hypothesis:
                return False
            metadata = hypothesis.get("metadata") or {}
            transitions = metadata.get("state_transitions", [])
            transitions.append(
                {
                    "from": hypothesis.get("state"),
                    "to": new_state,
                    "reason": reason,
                    "at": datetime.now(UTC).isoformat(),
                }
            )
            metadata["state_transitions"] = transitions
            with self._db_lock:
                conn = self._get_connection()
                conn.execute(
                    """
                    UPDATE hypotheses
                    SET state = ?, metadata = ?, updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (new_state, json.dumps(metadata, ensure_ascii=False), hypothesis_id),
                )
                conn.commit()
            self._emit_event(
                "cerebellum.hypothesis.state_changed",
                {"id": hypothesis_id, "from": hypothesis.get("state"), "to": new_state, "reason": reason},
            )
            return True
        except Exception as exc:
            logger.error("Failed to update hypothesis %s to %s: %s", hypothesis_id, new_state, exc)
            return False

    def expire_old_hypotheses(self, max_age_hours: int = 24) -> int:
        """Mark old 'proposed' hypotheses as 'expired'."""
        try:
            cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
            expired_ids: list[str] = []
            with self._db_lock:
                conn = self._get_connection()
                rows = conn.execute(
                    "SELECT id FROM hypotheses WHERE state = 'proposed' AND timestamp < ?",
                    (cutoff,),
                ).fetchall()
                expired_ids = [str(row["id"]) for row in rows]
                if expired_ids:
                    conn.execute(
                        """
                        UPDATE hypotheses
                        SET state = 'expired', updated_at = datetime('now')
                        WHERE state = 'proposed' AND timestamp < ?
                        """,
                        (cutoff,),
                    )
                    conn.commit()
            for hypothesis_id in expired_ids:
                self._emit_event(
                    "cerebellum.hypothesis.expired",
                    {"id": hypothesis_id, "cutoff": cutoff, "max_age_hours": max_age_hours},
                )
            return len(expired_ids)
        except Exception as exc:
            logger.error("Failed to expire old hypotheses: %s", exc)
            return 0

    def get_hypothesis_stats(self) -> dict:
        """Return counts by state, avg confidence, etc."""
        try:
            with self._db_lock:
                conn = self._get_connection()
                counts = {
                    row["state"]: row["count"]
                    for row in conn.execute(
                        "SELECT state, COUNT(*) AS count FROM hypotheses GROUP BY state"
                    ).fetchall()
                }
                aggregates = conn.execute(
                    """
                    SELECT COUNT(*) AS total,
                           AVG(confidence) AS avg_confidence,
                           AVG(utility) AS avg_utility,
                           AVG(generation_cost_usd) AS avg_cost
                    FROM hypotheses
                    """
                ).fetchone()
            return {
                "counts_by_state": counts,
                "total": int(aggregates["total"] or 0),
                "avg_confidence": round(float(aggregates["avg_confidence"] or 0.0), 3),
                "avg_utility": round(float(aggregates["avg_utility"] or 0.0), 3),
                "avg_cost": round(float(aggregates["avg_cost"] or 0.0), 3),
                "generated_at": datetime.now(UTC).isoformat(),
            }
        except Exception as exc:
            logger.error("Failed to compute hypothesis stats: %s", exc)
            return {
                "counts_by_state": {},
                "total": 0,
                "avg_confidence": 0.0,
                "avg_utility": 0.0,
                "avg_cost": 0.0,
                "generated_at": datetime.now(UTC).isoformat(),
            }

    def _get_recent_episodes(self, hours: int) -> list[dict[str, Any]]:
        if not self.hippocampus:
            return []
        since = datetime.now(UTC) - timedelta(hours=hours)
        for method_name, kwargs in (
            ("get_recent_episodes", {"since": since}),
            ("get_recent_episodes", {"hours": hours}),
            ("query", {"limit": 20, "since": since.isoformat()}),
        ):
            method = getattr(self.hippocampus, method_name, None)
            if not callable(method):
                continue
            try:
                result = method(**kwargs)
                return self._normalize_records(result)
            except TypeError:
                continue
            except Exception as exc:
                logger.warning("Failed fetching episodes via %s: %s", method_name, exc)
        return []

    def _get_recent_events(self, minutes: int) -> list[dict[str, Any]]:
        if not self.emitter:
            return []
        since = datetime.now(UTC) - timedelta(minutes=minutes)
        for method_name, kwargs in (
            ("get_recent_events", {"since": since}),
            ("get_recent_events", {"minutes": minutes}),
            ("list_recent_events", {"minutes": minutes}),
            ("query_recent_events", {"since": since.isoformat(), "limit": 50}),
        ):
            method = getattr(self.emitter, method_name, None)
            if not callable(method):
                continue
            try:
                result = method(**kwargs)
                return self._normalize_records(result)
            except TypeError:
                continue
            except Exception as exc:
                logger.warning("Failed fetching events via %s: %s", method_name, exc)
        return []

    def _normalize_records(self, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, dict):
            if isinstance(value.get("episodes"), list):
                return [self._json_safe(item) for item in value["episodes"]]
            if isinstance(value.get("events"), list):
                return [self._json_safe(item) for item in value["events"]]
            return [self._json_safe(value)]
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        return [self._json_safe(value)]

    def _json_safe(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {str(key): self._coerce_json_value(item) for key, item in value.items()}
        if hasattr(value, "to_dict") and callable(value.to_dict):
            return self._json_safe(value.to_dict())
        if hasattr(value, "__dict__"):
            return self._json_safe(vars(value))
        return {"value": self._coerce_json_value(value)}

    def _coerce_json_value(self, value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, list):
            return [self._coerce_json_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._coerce_json_value(item) for key, item in value.items()}
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _coerce_hypothesis(
        self,
        item: dict[str, Any],
        episodes: list,
        events: list,
        usage: dict[str, Any] | None = None,
    ) -> Hypothesis | None:
        try:
            title = str(item.get("title", "")).strip()
            description = str(item.get("description", "")).strip()
            plan = [str(step).strip() for step in item.get("plan", []) if str(step).strip()]
            tools_required = [
                str(tool).strip() for tool in item.get("tools_required", []) if str(tool).strip()
            ]
            if not title or not description or not plan:
                raise ValueError("Hypothesis missing required content")
            reversibility = str(item.get("reversibility", "partial")).strip().lower()
            if reversibility not in self.VALID_REVERSIBILITY:
                reversibility = "partial"
            context_summary = str(item.get("context_summary", "")).strip() or self._fallback_context_summary(
                episodes, events
            )
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            metadata.setdefault("source", "llm")
            metadata.setdefault("model_candidates", self.model_candidates)
            usage = usage or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            model_used = str(usage.get("model") or "")
            derived_cost = self._derive_cost(prompt_tokens, completion_tokens, model=model_used)
            logger.info(
                "Derived hypothesis cost from usage: model=%s prompt=%s completion=%s cost=%.6f",
                model_used,
                prompt_tokens,
                completion_tokens,
                derived_cost,
            )
            metadata.setdefault("cost_model", model_used)
            metadata.setdefault("cost_prompt_tokens", prompt_tokens)
            metadata.setdefault("cost_completion_tokens", completion_tokens)
            return Hypothesis(
                id=str(uuid.uuid4()),
                timestamp=datetime.now(UTC).isoformat(),
                title=title,
                description=description,
                confidence=self._clamp_float(item.get("confidence", 0.0)),
                utility=self._clamp_float(item.get("utility", 0.0)),
                generation_cost_usd=derived_cost,
                # TODO(Phase 5): estimate the real execution cost of the plan instead
                # of leaving this unknown at proposal-generation time.
                estimated_execution_cost_usd=None,
                reversibility=reversibility,
                plan=plan,
                tools_required=tools_required,
                context_summary=context_summary,
                state="proposed",
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning("Discarding invalid hypothesis payload %s: %s", item, exc)
            return None

    # USD per 1M tokens. Conservative upper-bound estimates — update quarterly.
    _MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
        # model_id: (prompt_price, completion_price)
        "openai/gpt-4o": (2.50, 10.00),
        "openai/gpt-4o-mini": (0.15, 0.60),
        "anthropic/claude-opus-4-7": (15.00, 75.00),
        "anthropic/claude-sonnet-4": (3.00, 15.00),
        "anthropic/claude-haiku-4": (0.80, 4.00),
        "google/gemini-2.0-flash": (0.10, 0.40),
    }
    _FALLBACK_PRICING = (5.00, 15.00)  # unknown model — assume pricey

    def _derive_cost(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: str | None = None,
    ) -> float:
        """Estimate hypothesis-generation cost in USD using per-model pricing.

        Returns a value clamped to [0.0, 1.0] because downstream policy
        thresholds (arbiter) treat cost as a unit-interval score.
        """
        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        chosen_model = (model or "").strip()
        if chosen_model not in self._MODEL_PRICING_USD_PER_MTOK and self.model_candidates:
            chosen_model = self.model_candidates[0]
        prompt_price, completion_price = self._MODEL_PRICING_USD_PER_MTOK.get(
            chosen_model, self._FALLBACK_PRICING
        )
        cost_usd = (prompt * prompt_price + completion * completion_price) / 1_000_000.0
        return max(0.0, min(1.0, round(cost_usd, 6)))

    def _fallback_context_summary(self, episodes: list, events: list) -> str:
        return (
            f"Derived from {len(episodes)} recent episodes and {len(events)} recent events observed by CEREBELLUM."
        )

    def _clamp_float(self, value: Any) -> float:
        try:
            return round(max(0.0, min(1.0, float(value))), 3)
        except (TypeError, ValueError):
            return 0.0

    def _is_duplicate(self, hypothesis: Hypothesis, existing: list[dict[str, Any]]) -> bool:
        """Duplicate if exact title match OR Jaccard token similarity > 0.8.

        The old substring-in-description rule falsely flagged almost any
        hypothesis whose title happened to be a common phrase.
        """
        candidate_title = hypothesis.title.strip().lower()
        if not candidate_title:
            return False
        candidate_tokens = self._tokenize_for_dedup(candidate_title)

        for item in existing:
            existing_title = str(item.get("title", "")).strip().lower()
            if not existing_title:
                continue
            if existing_title == candidate_title:
                return True
            existing_tokens = self._tokenize_for_dedup(existing_title)
            if self._jaccard(candidate_tokens, existing_tokens) > 0.8:
                return True
        return False

    @staticmethod
    def _tokenize_for_dedup(text: str) -> set[str]:
        import re as _re

        return {tok for tok in _re.findall(r"[a-z0-9]+", text.lower()) if len(tok) > 2}

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        intersection = len(a & b)
        union = len(a | b)
        return intersection / union if union else 0.0

    def _store_hypothesis(self, hypothesis: Hypothesis) -> bool:
        try:
            with self._db_lock:
                conn = self._get_connection()
                conn.execute(
                    """
                    INSERT INTO hypotheses (
                        id, timestamp, title, description, confidence, utility,
                        generation_cost_usd, estimated_execution_cost_usd,
                        reversibility, plan, tools_required, context_summary, state, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hypothesis.id,
                        hypothesis.timestamp,
                        hypothesis.title,
                        hypothesis.description,
                        hypothesis.confidence,
                        hypothesis.utility,
                        hypothesis.generation_cost_usd,
                        hypothesis.estimated_execution_cost_usd,
                        hypothesis.reversibility,
                        json.dumps(hypothesis.plan, ensure_ascii=False),
                        json.dumps(hypothesis.tools_required, ensure_ascii=False),
                        hypothesis.context_summary,
                        hypothesis.state,
                        json.dumps(hypothesis.metadata, ensure_ascii=False),
                    ),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("Failed to store hypothesis %s: %s", hypothesis.id, exc)
            return False

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        if "generation_cost_usd" not in result or result.get("generation_cost_usd") is None:
            result["generation_cost_usd"] = result.get("cost") or 0.0
        if "estimated_execution_cost_usd" not in result:
            result["estimated_execution_cost_usd"] = None
        result.pop("cost", None)
        result["plan"] = self._parse_json_column(result.get("plan"), [])
        result["tools_required"] = self._parse_json_column(result.get("tools_required"), [])
        result["metadata"] = self._parse_json_column(result.get("metadata"), {})
        return result

    def _parse_json_column(self, value: Any, fallback: Any) -> Any:
        try:
            return json.loads(value) if value else fallback
        except Exception:
            # Stored metadata may be partially corrupted; log at debug and keep reads resilient with a safe fallback.
            logger.debug("Falling back for malformed JSON column value", exc_info=True)
            return fallback

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.emitter:
            return
        try:
            if hasattr(self.emitter, "emit") and callable(self.emitter.emit):
                self.emitter.emit(event_type, payload=payload, actor="cerebellum.proposer", context={"source": "phase3"})
                return
            if hasattr(self.emitter, "publish") and callable(self.emitter.publish):
                self.emitter.publish(event_type, payload)
        except Exception as exc:
            logger.warning("Failed to emit event %s: %s", event_type, exc)


PrefrontalCortex = Proposer
