from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import kuzu

try:
    from .http_safe import _safe_opener
except ImportError:
    from http_safe import _safe_opener


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LLMResponse:
    text: str
    raw: dict[str, Any]


class Hippocampus:
    """Causal memory engine using KuzuDB graph + Qdrant vectors."""

    GRAPH_DIR = Path("/home/josh/.openclaw/cerebellum/graph")
    DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o"
    DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
    _READ_ONLY_QUERY_PREFIXES = ("MATCH", "CALL", "UNWIND", "WITH", "RETURN", "EXPLAIN", "PROFILE")
    _READ_ONLY_BLOCKED_KEYWORDS = (
        "CREATE",
        "MERGE",
        "DELETE",
        "DETACH",
        "SET",
        "DROP",
        "COPY",
        "LOAD",
        "REMOVE",
        "INSTALL",
        "ALTER",
        "ATTACH",
        "USE",
        "IMPORT",
        "EXPORT",
        "UPDATE",
        "UPSERT",
    )

    def __init__(self, config_path: str):
        self.config_path = Path(config_path).expanduser()
        self.config = self._load_config(self.config_path)
        configured_graph_path = Path(
            self.config.get("hippocampus", {}).get("graph_path", self.GRAPH_DIR)
        ).expanduser()
        if configured_graph_path.suffix:
            self.graph_dir = configured_graph_path.parent
            self.db_path = configured_graph_path
        else:
            self.graph_dir = configured_graph_path
            self.db_path = configured_graph_path / "hippocampus.kuzu"
        self.graph_dir.mkdir(parents=True, exist_ok=True)

        self.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.openrouter_url = self.config.get("hippocampus", {}).get("openrouter_url", self.DEFAULT_OPENROUTER_URL)
        self.openrouter_model = self.config.get("hippocampus", {}).get("openrouter_model", self.DEFAULT_OPENROUTER_MODEL)

        self._db = kuzu.Database(str(self.db_path))
        self._thread_local = threading.local()
        self._schema_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._connection_lock = threading.RLock()
        self._tracked_connections: list[kuzu.Connection] = []
        self._closed = False

        self._ensure_schema()
        self._readonly_db = kuzu.Database(str(self.db_path), read_only=True)
        atexit.register(self.close)

    def add_event(self, event: dict[str, Any]) -> None:
        normalized = self._normalize_event(event)
        entities = self.extract_entities(normalized["payload"])
        payload_json = json.dumps(normalized["payload"], sort_keys=True)

        try:
            with self._write_lock:
                self._run(
                    """
                    MERGE (event:Event {id: $id})
                    SET event.timestamp = $timestamp,
                        event.type = $type,
                        event.payload = $payload,
                        event.actor = $actor
                    """,
                    {
                        "id": normalized["id"],
                        "timestamp": normalized["timestamp"],
                        "type": normalized["type"],
                        "payload": payload_json,
                        "actor": normalized["actor"],
                    },
                )

                for entity in entities:
                    self._upsert_entity(entity, normalized["timestamp"])
        except Exception as exc:
            logger.error("Failed to add event %s: %s", normalized.get("id"), exc)
            raise

    def create_episode(self, events: list[dict[str, Any]]) -> str:
        if not events:
            raise ValueError("create_episode requires at least one event")

        normalized_events = [self._normalize_event(event) for event in events]
        normalized_events.sort(key=lambda item: item["timestamp"])
        event_ids = [event["id"] for event in normalized_events]
        episode_id = f"episode:{hashlib.sha1('|'.join(event_ids).encode('utf-8')).hexdigest()[:16]}"

        title, summary = self._summarize_episode(normalized_events)
        start_time = normalized_events[0]["timestamp"]
        end_time = normalized_events[-1]["timestamp"]

        try:
            with self._write_lock:
                for event in normalized_events:
                    self.add_event(event)

                self._run(
                    """
                    MERGE (episode:Episode {id: $id})
                    SET episode.title = $title,
                        episode.summary = $summary,
                        episode.start_time = $start_time,
                        episode.end_time = $end_time,
                        episode.event_count = $event_count
                    """,
                    {
                        "id": episode_id,
                        "title": title,
                        "summary": summary,
                        "start_time": start_time,
                        "end_time": end_time,
                        "event_count": len(normalized_events),
                    },
                )

                entity_map: dict[str, dict[str, str]] = {}
                for event in normalized_events:
                    self._run(
                        """
                        MATCH (event:Event {id: $event_id}), (episode:Episode {id: $episode_id})
                        MERGE (event)-[:BELONGS_TO]->(episode)
                        """,
                        {"event_id": event["id"], "episode_id": episode_id},
                    )

                    for entity in self.extract_entities(event["payload"]):
                        entity_key = f"{entity['type']}::{entity['name'].lower()}"
                        entity_map.setdefault(entity_key, entity)

                for entity in entity_map.values():
                    entity_id = self._entity_id(entity["type"], entity["name"])
                    self._upsert_entity(entity, end_time)
                    self._run(
                        """
                        MATCH (episode:Episode {id: $episode_id}), (entity:Entity {id: $entity_id})
                        MERGE (episode)-[:CONTAINS]->(entity)
                        """,
                        {"episode_id": episode_id, "entity_id": entity_id},
                    )
        except Exception as exc:
            logger.error("Failed to create episode from %d events: %s", len(events), exc)
            raise

        return episode_id

    def _summarize_episode(self, events: list[dict[str, Any]]) -> tuple[str, str]:
        event_lines = [
            f"- {event['timestamp']} | {event['type']} | actor={event['actor']} | payload={json.dumps(event['payload'], sort_keys=True)}"
            for event in events[:25]
        ]
        prompt = (
            "Given these system events, generate a concise title (max 20 words) and summary "
            "(max 100 words) describing what happened. Return strict JSON with keys title and summary.\n\n"
            + "\n".join(event_lines)
        )

        try:
            response = self._call_llm(prompt)
            parsed = self._extract_json_object(response.text)
            title = str(parsed.get("title", "")).strip()
            summary = str(parsed.get("summary", "")).strip()
            if title and summary:
                return title[:120], summary[:600]
            raise ValueError("LLM summary response missing title or summary")
        except Exception as exc:
            logger.warning("LLM summarization failed; using heuristic fallback: %s", exc)
            return self._heuristic_episode_summary(events)

    def extract_entities(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        entities: dict[str, dict[str, str]] = {}
        payload_text = json.dumps(payload, sort_keys=True, default=str)

        file_pattern = re.compile(r"(?:/[\w.\-]+)+|[\w.\-/]+\.(?:py|ts|tsx|js|jsx|json|md|yaml|yml|log|txt|sql)")
        for match in file_pattern.findall(payload_text):
            cleaned = match.strip('"\' ,')
            if len(cleaned) >= 3:
                entities[self._entity_key("file", cleaned)] = {
                    "name": cleaned,
                    "type": "file",
                    "description": "File path referenced in event payload",
                }

        for key in ("project", "repo", "repository", "workspace"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                entities[self._entity_key("project", value)] = {
                    "name": value.strip(),
                    "type": "project",
                    "description": f"Project value from payload field '{key}'",
                }

        for key in ("service", "daemon", "job", "component", "module"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                entities[self._entity_key("service", value)] = {
                    "name": value.strip(),
                    "type": "service",
                    "description": f"Service or component from payload field '{key}'",
                }

        for key in ("user", "owner", "author", "assignee", "requester"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                entities[self._entity_key("person", value)] = {
                    "name": value.strip(),
                    "type": "person",
                    "description": f"Person referenced by payload field '{key}'",
                }

        for key, value in payload.items():
            if isinstance(value, (int, float)) and any(token in key.lower() for token in ("latency", "count", "total", "usage", "temp", "score", "duration", "memory")):
                metric_name = f"{key}={value}"
                entities[self._entity_key("metric", metric_name)] = {
                    "name": metric_name,
                    "type": "metric",
                    "description": f"Metric extracted from numeric payload field '{key}'",
                }

        error_pattern = re.compile(r"\b(error|exception|timeout|failed|failure|traceback|crash)\b", re.IGNORECASE)
        if error_pattern.search(payload_text):
            snippet = re.sub(r"\s+", " ", payload_text)[:160]
            entities[self._entity_key("error", snippet)] = {
                "name": snippet,
                "type": "error",
                "description": "Error-related payload content",
            }

        return list(entities.values())

    def mine_causal_edges(self, window_hours: int = 168) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        discovered: list[dict[str, Any]] = []

        try:
            rows = self._fetch_all_read_only(
                """
                MATCH (event:Event)
                WHERE event.timestamp >= $since
                RETURN event.id AS id,
                       event.timestamp AS timestamp,
                       event.type AS type,
                       event.actor AS actor,
                       event.payload AS payload
                ORDER BY event.timestamp ASC
                """,
                {"since": since},
            )
        except Exception as exc:
            logger.error("Failed to fetch events for causal mining: %s", exc)
            return []

        if len(rows) < 2:
            return []

        parsed_rows = []
        for row in rows:
            try:
                parsed_rows.append(
                    {
                        **row,
                        "dt": self._parse_timestamp(str(row["timestamp"])),
                    }
                )
            except Exception as exc:
                logger.debug("Skipping unparsable event row during causal mining: %s", exc)

        if len(parsed_rows) > 500:
            logger.warning("Causal mining capped to 500 events (had %d)", len(parsed_rows))
            parsed_rows = parsed_rows[:500]

        support_counts: Counter[tuple[str, str]] = Counter()
        source_counts: Counter[str] = Counter()
        one_hour = timedelta(hours=1)

        for index, current in enumerate(parsed_rows):
            source_type = str(current["type"])
            source_counts[source_type] += 1
            for follower in parsed_rows[index + 1 : index + 51]:
                delta = follower["dt"] - current["dt"]
                if delta <= timedelta(0):
                    continue
                if delta > one_hour:
                    break
                follower_type = str(follower["type"])
                if self._event_types_change_significantly(source_type, follower_type):
                    break
                support_counts[(source_type, follower_type)] += 1

        for (source_type, target_type), support in support_counts.items():
            if support < 5:
                continue

            confidence = round(support / max(source_counts[source_type], 1), 4)
            edge_id = f"causal:{hashlib.sha1(f'{source_type}->{target_type}'.encode('utf-8')).hexdigest()[:16]}"
            timestamp = datetime.now(timezone.utc).isoformat()

            try:
                with self._write_lock:
                    self._run(
                        """
                        MERGE (edge:CausalEdge {id: $id})
                        SET edge.source_type = $source_type,
                            edge.target_type = $target_type,
                            edge.support = $support,
                            edge.confidence = $confidence,
                            edge.first_seen = COALESCE(edge.first_seen, $timestamp),
                            edge.last_seen = $timestamp
                        """,
                        {
                            "id": edge_id,
                            "source_type": source_type,
                            "target_type": target_type,
                            "support": int(support),
                            "confidence": float(confidence),
                            "timestamp": timestamp,
                        },
                    )

                    target_entity = {
                        "name": target_type,
                        "type": "service",
                        "description": f"Event type target for causal pattern {source_type} -> {target_type}",
                    }
                    self._upsert_entity(target_entity, timestamp)
                    self._run(
                        """
                        MATCH (edge:CausalEdge {id: $edge_id}), (entity:Entity {id: $entity_id})
                        MERGE (edge)-[:CAUSES]->(entity)
                        """,
                        {
                            "edge_id": edge_id,
                            "entity_id": self._entity_id(target_entity["type"], target_entity["name"]),
                        },
                    )
            except Exception as exc:
                logger.error("Failed to persist causal edge %s -> %s: %s", source_type, target_type, exc)
                continue

            discovered.append(
                {
                    "id": edge_id,
                    "source_type": source_type,
                    "target_type": target_type,
                    "support": int(support),
                    "confidence": float(confidence),
                }
            )

        return discovered

    def query(self, natural_language: str) -> dict[str, Any]:
        fallback = self._heuristic_query(natural_language)
        llm_query = self._generate_query_from_nl(natural_language)
        if not llm_query:
            return fallback

        if not self._is_safe_read_query(llm_query):
            logger.warning("Rejected unsafe generated query: %s", llm_query)
            return fallback

        try:
            if not self._is_safe_read_query(llm_query):
                logger.warning("Rejected unsafe generated query before execution: %s", llm_query)
                return fallback
            rows = self._fetch_all_read_only(llm_query)
            return {"ok": True, "mode": "llm", "query": llm_query, "rows": rows}
        except Exception as exc:
            logger.warning("Generated query failed; falling back to heuristics: %s", exc)
            fallback["generated_query"] = llm_query
            return fallback

    def get_recent_episodes(self, limit: int = 10) -> list[dict[str, Any]]:
        try:
            episodes = self._fetch_all_read_only(
                """
                MATCH (episode:Episode)
                RETURN episode.id AS id,
                       episode.title AS title,
                       episode.summary AS summary,
                       episode.start_time AS start_time,
                       episode.end_time AS end_time,
                       episode.event_count AS event_count
                ORDER BY episode.start_time DESC
                LIMIT $limit
                """,
                {"limit": int(limit)},
            )
        except Exception as exc:
            logger.error("Failed to load recent episodes: %s", exc)
            return []

        for episode in episodes:
            try:
                episode["entities"] = self._fetch_all_read_only(
                    """
                    MATCH (episode:Episode {id: $episode_id})-[:CONTAINS]->(entity:Entity)
                    RETURN entity.id AS id,
                           entity.name AS name,
                           entity.type AS type,
                           entity.description AS description,
                           entity.last_seen AS last_seen
                    ORDER BY entity.type, entity.name
                    """,
                    {"episode_id": episode["id"]},
                )
            except Exception as exc:
                logger.warning("Failed to load entities for episode %s: %s", episode.get("id"), exc)
                episode["entities"] = []

        return episodes

    def _load_config(self, config_path: Path) -> dict[str, Any]:
        try:
            return json.loads(config_path.read_text())
        except FileNotFoundError:
            logger.warning("Config file %s not found; using defaults", config_path)
            return {}
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON config at %s: %s", config_path, exc)
            raise

    def _ensure_schema(self) -> None:
        schema_queries = [
            """
            CREATE NODE TABLE IF NOT EXISTS Event(
                id STRING PRIMARY KEY,
                timestamp STRING,
                type STRING,
                payload STRING,
                actor STRING
            );
            """,
            """
            CREATE NODE TABLE IF NOT EXISTS Episode(
                id STRING PRIMARY KEY,
                title STRING,
                summary STRING,
                start_time STRING,
                end_time STRING,
                event_count INT64
            );
            """,
            """
            CREATE NODE TABLE IF NOT EXISTS Entity(
                id STRING PRIMARY KEY,
                name STRING,
                type STRING,
                description STRING,
                last_seen STRING
            );
            """,
            """
            CREATE NODE TABLE IF NOT EXISTS CausalEdge(
                id STRING PRIMARY KEY,
                source_type STRING,
                target_type STRING,
                support INT64,
                confidence FLOAT,
                first_seen STRING,
                last_seen STRING
            );
            """,
            "CREATE REL TABLE IF NOT EXISTS BELONGS_TO(FROM Event TO Episode);",
            "CREATE REL TABLE IF NOT EXISTS CONTAINS(FROM Episode TO Entity);",
            "CREATE REL TABLE IF NOT EXISTS CAUSES(FROM CausalEdge TO Entity);",
        ]

        try:
            with self._schema_lock:
                for query in schema_queries:
                    self._run(query)
        except Exception as exc:
            logger.error("Failed to initialize Hippocampus schema: %s", exc)
            raise

    def _get_connection(self) -> kuzu.Connection:
        connection = getattr(self._thread_local, "connection", None)
        if connection is None or getattr(connection, "is_closed", False):
            connection = kuzu.Connection(self._db)
            self._thread_local.connection = connection
            self._track_connection(connection)
        return connection

    def _get_read_connection(self) -> kuzu.Connection:
        connection = getattr(self._thread_local, "read_connection", None)
        if connection is None or getattr(connection, "is_closed", False):
            connection = kuzu.Connection(self._readonly_db)
            self._thread_local.read_connection = connection
            self._track_connection(connection)
        return connection

    def _track_connection(self, connection: kuzu.Connection) -> None:
        with self._connection_lock:
            if all(existing is not connection for existing in self._tracked_connections):
                self._tracked_connections.append(connection)

    def _execute(self, query: str, parameters: dict[str, Any] | None = None) -> kuzu.QueryResult:
        conn = self._get_connection()
        cleaned_query = query.strip()
        return conn.execute(cleaned_query, parameters or {})

    def _run(self, query: str, parameters: dict[str, Any] | None = None) -> None:
        result = self._execute(query, parameters)
        result.close()

    def _fetch_all(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self._execute(query, parameters)
        try:
            columns = result.get_column_names()
            if columns is None:
                return []
            rows: list[dict[str, Any]] = []
            while result.has_next():
                values = result.get_next()
                rows.append(dict(zip(columns, values, strict=False)))
            return rows
        finally:
            result.close()

    def _fetch_all_read_only(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        cleaned_query = self._strip_query_comments(query).strip()
        if not self._is_safe_read_query(cleaned_query):
            raise ValueError("Refusing to execute non-read-only query")
        conn = self._get_read_connection()
        transaction_open = False
        try:
            begin_result = conn.execute("BEGIN TRANSACTION")
            begin_result.close()
            transaction_open = True
            result = conn.execute(cleaned_query, parameters or {})
            try:
                columns = result.get_column_names()
                rows: list[dict[str, Any]] = []
                while result.has_next():
                    values = result.get_next()
                    rows.append(dict(zip(columns, values, strict=False)))
                return rows
            finally:
                result.close()
        finally:
            if transaction_open:
                rollback_result = conn.execute("ROLLBACK")
                rollback_result.close()

    def _upsert_entity(self, entity: dict[str, str], timestamp: str) -> None:
        self._run(
            """
            MERGE (entity:Entity {id: $id})
            SET entity.name = $name,
                entity.type = $type,
                entity.description = $description,
                entity.last_seen = $last_seen
            """,
            {
                "id": self._entity_id(entity["type"], entity["name"]),
                "name": entity["name"],
                "type": entity["type"],
                "description": entity["description"],
                "last_seen": timestamp,
            },
        )

    def _normalize_event(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {"raw": payload}
        elif not isinstance(payload, dict):
            payload = {"raw": payload}

        timestamp = str(event.get("timestamp") or datetime.now(timezone.utc).isoformat())
        self._parse_timestamp(timestamp)

        normalized = {
            "id": str(event.get("id") or self._hash_payload(payload, timestamp)),
            "timestamp": timestamp,
            "type": str(event.get("type") or "unknown"),
            "payload": payload,
            "actor": str(event.get("actor") or "system"),
        }
        return normalized

    def _heuristic_episode_summary(self, events: list[dict[str, Any]]) -> tuple[str, str]:
        event_types = [str(event["type"]) for event in events]
        actors = sorted({str(event["actor"]) for event in events if event.get("actor")})
        type_counts = Counter(event_types).most_common(3)
        dominant_types = ", ".join(event_type for event_type, _ in type_counts)
        start_time = events[0]["timestamp"]
        end_time = events[-1]["timestamp"]
        title = f"{dominant_types or 'System activity'} episode"
        summary = (
            f"{len(events)} events occurred between {start_time} and {end_time}. "
            f"Primary event types: {dominant_types or 'unknown'}. "
            f"Actors involved: {', '.join(actors) if actors else 'system only'}."
        )
        return title[:120], summary[:600]

    def _heuristic_query(self, natural_language: str) -> dict[str, Any]:
        lowered = natural_language.lower()
        try:
            if any(token in lowered for token in ("recent episode", "latest episode", "recent episodes")):
                return {"ok": True, "mode": "heuristic", "query": "recent_episodes", "rows": self.get_recent_episodes()}

            if any(token in lowered for token in ("causal", "cause", "causes", "pattern")):
                rows = self._fetch_all_read_only(
                    """
                    MATCH (edge:CausalEdge)
                    RETURN edge.id AS id,
                           edge.source_type AS source_type,
                           edge.target_type AS target_type,
                           edge.support AS support,
                           edge.confidence AS confidence,
                           edge.last_seen AS last_seen
                    ORDER BY edge.support DESC, edge.confidence DESC
                    LIMIT 25
                    """
                )
                return {"ok": True, "mode": "heuristic", "query": "causal_edges", "rows": rows}

            event_match = re.search(r"(?:event type|events about|events for)\s+([\w.:-]+)", lowered)
            if event_match:
                event_type = event_match.group(1)
                rows = self._fetch_all_read_only(
                    """
                    MATCH (event:Event)
                    WHERE lower(event.type) = lower($event_type)
                    RETURN event.id AS id,
                           event.timestamp AS timestamp,
                           event.type AS type,
                           event.actor AS actor,
                           event.payload AS payload
                    ORDER BY event.timestamp DESC
                    LIMIT 25
                    """,
                    {"event_type": event_type},
                )
                return {"ok": True, "mode": "heuristic", "query": "events_by_type", "rows": rows}

            rows = self._fetch_all_read_only(
                """
                MATCH (event:Event)
                RETURN event.id AS id,
                       event.timestamp AS timestamp,
                       event.type AS type,
                       event.actor AS actor
                ORDER BY event.timestamp DESC
                LIMIT 10
                """
            )
            return {"ok": True, "mode": "heuristic", "query": "recent_events", "rows": rows}
        except Exception as exc:
            logger.error("Heuristic query failed for '%s': %s", natural_language, exc)
            return {"ok": False, "mode": "heuristic", "error": str(exc), "rows": []}

    def _generate_query_from_nl(self, natural_language: str) -> str | None:
        prompt = f"""
You translate natural language into a safe read-only Kuzu Cypher query.

Schema:
- Event(id, timestamp, type, payload, actor)
- Episode(id, title, summary, start_time, end_time, event_count)
- Entity(id, name, type, description, last_seen)
- CausalEdge(id, source_type, target_type, support, confidence, first_seen, last_seen)
- (Event)-[:BELONGS_TO]->(Episode)
- (Episode)-[:CONTAINS]->(Entity)
- (CausalEdge)-[:CAUSES]->(Entity)

Rules:
- Return strict JSON with a single key named query.
- Query must be read-only and start with MATCH, CALL, UNWIND, WITH, RETURN, EXPLAIN, or PROFILE.
- Never include comments or semicolons.
- Never use CREATE, MERGE, DELETE, DETACH, SET, DROP, COPY, LOAD, REMOVE, INSTALL, ALTER, ATTACH, USE, IMPORT, EXPORT, UPDATE, or UPSERT.
- Prefer LIMIT 25 unless the user requests more.

User request: {natural_language}
""".strip()

        try:
            response = self._call_llm(prompt)
            parsed = self._extract_json_object(response.text)
            query = self._strip_query_comments(str(parsed.get("query", ""))).strip()
            return query or None
        except Exception as exc:
            logger.warning("Failed to generate NL query via LLM: %s", exc)
            return None

    def _event_types_change_significantly(self, source_type: str, follower_type: str) -> bool:
        source_tokens = {token for token in re.split(r"[^a-z0-9]+", source_type.lower()) if token}
        follower_tokens = {token for token in re.split(r"[^a-z0-9]+", follower_type.lower()) if token}
        if not source_tokens or not follower_tokens:
            return False
        return source_tokens.isdisjoint(follower_tokens)

    def _call_llm(self, prompt: str) -> LLMResponse:
        if not self.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")

        payload = {
            "model": self.openrouter_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        request = urllib.request.Request(
            self.openrouter_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "HTTP-Referer": "https://localhost/cerebellum",
                "X-Title": "CEREBELLUM",
            },
            method="POST",
        )

        try:
            with _safe_opener.open(request, timeout=60) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

        try:
            text = self._read_nested_key(response_payload, "choices.0.message.content")
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("OpenRouter response missing text")
        except TypeError as exc:
            raise RuntimeError(f"OpenRouter response parsing failed: {exc}") from exc
        return LLMResponse(text=text.strip(), raw=response_payload)

    def _read_nested_key(self, payload: dict[str, Any], key_path: str) -> Any:
        current: Any = payload
        for part in key_path.split("."):
            if isinstance(current, list):
                current = current[int(part)]
            else:
                current = current[part]
        return current

    def _extract_json_object(self, text: str) -> dict[str, Any]:
        if not text:
            raise ValueError("Empty LLM response")

        stripped = text.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", stripped, re.DOTALL)
            if not match:
                raise
            return json.loads(match.group(0))

    def _is_safe_read_query(self, query: str) -> bool:
        if not query or not query.strip():
            return False

        candidate = self._strip_query_comments(query).strip()
        if not candidate:
            return False

        if ";" in candidate:
            return False

        for keyword in self._READ_ONLY_BLOCKED_KEYWORDS:
            if re.search(r"\b" + keyword + r"\b", candidate, re.IGNORECASE):
                return False

        first_word = candidate.split()[0].upper() if candidate.split() else ""
        return first_word in self._READ_ONLY_QUERY_PREFIXES

    def _strip_query_comments(self, query: str) -> str:
        without_block_comments = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL)
        return re.sub(r"--.*$", "", without_block_comments, flags=re.MULTILINE)

    def _hash_payload(self, payload: dict[str, Any], timestamp: str) -> str:
        digest = hashlib.sha1(f"{timestamp}|{json.dumps(payload, sort_keys=True, default=str)}".encode("utf-8")).hexdigest()
        return f"event:{digest[:16]}"

    def _entity_id(self, entity_type: str, name: str) -> str:
        digest = hashlib.sha1(f"{entity_type}:{name.lower()}".encode("utf-8")).hexdigest()
        return f"entity:{digest[:16]}"

    def _entity_key(self, entity_type: str, name: str) -> str:
        return f"{entity_type}:{name.strip().lower()}"

    def _parse_timestamp(self, value: str) -> datetime:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def close(self) -> None:
        with self._connection_lock:
            if self._closed:
                return
            self._closed = True
            tracked_connections = list(self._tracked_connections)
            self._tracked_connections.clear()

        for attribute_name in ("read_connection", "connection"):
            thread_connection = getattr(self._thread_local, attribute_name, None)
            if thread_connection is not None and all(existing is not thread_connection for existing in tracked_connections):
                tracked_connections.append(thread_connection)

        for index, connection in enumerate(tracked_connections):
            close = getattr(connection, "close", None)
            if not callable(close):
                logger.debug("Kuzu connection %d does not expose close(); skipping shutdown", index)
                continue

            try:
                close()
            except Exception as exc:
                # Connection cleanup is best-effort during shutdown; keep going so every handle gets a chance to close.
                logger.debug("Failed to close Kuzu thread connection %d cleanly: %s", index, exc)

        for db_name in ("_readonly_db", "_db"):
            db = getattr(self, db_name, None)
            if db is None:
                continue

            close = getattr(db, "close", None)
            if not callable(close):
                logger.debug("KuzuDB %s does not expose close(); skipping shutdown", db_name)
                continue

            try:
                close()
            except Exception as exc:
                logger.error("Failed to close KuzuDB handle %s: %s", db_name, exc)
