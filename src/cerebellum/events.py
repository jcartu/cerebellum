from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

logger = logging.getLogger(__name__)


class CerebellumEventEmitter:
    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self.config = self._load_config(self.config_path)
        self.db_path = Path(self.config["sqlite"]["events_db"]).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db_lock = threading.Lock()
        self._sqlite = sqlite3.connect(self.db_path, check_same_thread=False)
        self._sqlite.row_factory = sqlite3.Row
        self._checkpoint_interval_seconds = 300
        self._checkpoint_stop = threading.Event()
        self._checkpoint_thread: threading.Thread | None = None
        self._close_publish_timeout_seconds = 5.0
        self._configure_sqlite()

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

        self._nc: NATS | None = None
        self._js: Any | None = None
        self._nats_ready = False
        self._subscription_futures: list[Future[Any]] = []
        self._subscription_futures_lock = threading.RLock()
        self._max_subscription_futures = 128
        self._inflight_publishes: list[Future[Any]] = []
        self._inflight_lock = threading.RLock()

        self._connect_to_nats()
        self._start_checkpoint_worker()

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        actor: str = "system",
        context: dict[str, Any] | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        event = {
            "id": event_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "type": event_type,
            "payload": payload,
            "actor": actor,
            "context": context or {},
        }

        self._write_event(event)

        if self._nats_ready:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._publish_event(event), self._loop)
                with self._inflight_lock:
                    self._inflight_publishes.append(fut)
                    # Cap list size — drop refs to any already-completed futures.
                    if len(self._inflight_publishes) > 256:
                        self._inflight_publishes = [
                            f for f in self._inflight_publishes if not f.done()
                        ]

                def _on_done(f: Future[Any], _eid: str = event_id) -> None:
                    try:
                        f.result()
                    except Exception as exc:
                        logger.error("NATS publish failed for event %s: %s", _eid, exc)
                    finally:
                        with self._inflight_lock:
                            try:
                                self._inflight_publishes.remove(f)
                            except ValueError:
                                # Another cleanup path may have already removed this completed future.
                                logger.debug("Publish future for event %s already removed from inflight list", _eid)

                fut.add_done_callback(_on_done)
            except RuntimeError:
                logger.debug("NATS loop unavailable, stored event %s in SQLite only", event_id)
        else:
            logger.debug("NATS unavailable, stored event %s in SQLite only", event_id)
        return event_id

    def write_event(self, event: dict[str, Any]) -> None:
        required_keys = {"id", "timestamp", "type", "payload", "actor", "context"}
        if not required_keys.issubset(event):
            missing = sorted(required_keys.difference(event))
            raise ValueError(f"Event payload missing required keys: {', '.join(missing)}")
        normalized_event = {
            "id": str(event["id"]),
            "timestamp": str(event["timestamp"] or datetime.now(UTC).isoformat()),
            "type": str(event["type"] or "unknown"),
            "payload": event["payload"] if isinstance(event["payload"], dict) else {},
            "actor": str(event["actor"] or "system"),
            "context": event["context"] if isinstance(event["context"], dict) else {},
        }
        self._write_event(normalized_event, insert_mode="OR IGNORE")

    def query(
        self,
        types: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT id, timestamp, type, payload, actor, context FROM events"
        conditions: list[str] = []
        params: list[Any] = []

        if types:
            placeholders = ", ".join("?" for _ in types)
            conditions.append(f"type IN ({placeholders})")
            params.extend(types)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since.astimezone(UTC).isoformat())

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        try:
            with self._db_lock:
                rows = self._sqlite.execute(sql, params).fetchall()
            return [self._row_to_event(row) for row in rows]
        except Exception as exc:
            logger.error("Failed to query events: %s", exc)
            return []

    def subscribe(self, callback: Callable[[dict[str, Any]], Any]) -> None:
        if not self._nats_ready:
            logger.warning("NATS unavailable; subscription skipped")
            return

        future = asyncio.run_coroutine_threadsafe(self._subscribe(callback), self._loop)
        with self._subscription_futures_lock:
            # Reap completed futures first to avoid unbounded growth.
            self._subscription_futures = [
                f for f in self._subscription_futures if not f.done()
            ]
            if len(self._subscription_futures) >= self._max_subscription_futures:
                # Drop oldest reference if somehow at cap.
                self._subscription_futures.pop(0)
            self._subscription_futures.append(future)

    def close(self) -> None:
        self._checkpoint_stop.set()
        if self._checkpoint_thread is not None:
            self._checkpoint_thread.join(timeout=2)
        try:
            if self._nats_ready and self._nc:
                future = asyncio.run_coroutine_threadsafe(self._nc.drain(), self._loop)
                future.result(timeout=5)
        except Exception as exc:
            logger.warning("Failed to drain NATS connection cleanly: %s", exc)
        finally:
            self._nats_ready = False

        remaining_inflight = self._wait_for_inflight_publishes(self._close_publish_timeout_seconds)
        if remaining_inflight:
            logger.warning(
                "Event emitter closed with %d publish(es) still inflight after %.1fs timeout",
                remaining_inflight,
                self._close_publish_timeout_seconds,
            )

        with self._db_lock:
            self._sqlite.close()

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=2)

    def _load_config(self, config_path: Path) -> dict[str, Any]:
        try:
            return json.loads(config_path.read_text())
        except FileNotFoundError:
            logger.error("Config file not found: %s", config_path)
            raise
        except json.JSONDecodeError as exc:
            logger.error("Invalid config JSON in %s: %s", config_path, exc)
            raise

    def _configure_sqlite(self) -> None:
        try:
            with self._db_lock:
                self._sqlite.execute("PRAGMA journal_mode=WAL;")
                self._sqlite.execute("PRAGMA synchronous=NORMAL;")
                self._sqlite.execute("PRAGMA wal_autocheckpoint=1000;")
                self._sqlite.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id TEXT PRIMARY KEY,
                        timestamp TEXT NOT NULL,
                        type TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        context TEXT NOT NULL
                    )
                    """
                )
                self._sqlite.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC)"
                )
                self._sqlite.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_type_timestamp ON events(type, timestamp DESC)"
                )
                self._sqlite.commit()
        except Exception as exc:
            logger.error("Failed to configure SQLite backing store: %s", exc)
            raise

    def _start_checkpoint_worker(self) -> None:
        self._checkpoint_thread = threading.Thread(
            target=self._checkpoint_worker,
            name="cerebellum-events-wal-checkpoint",
            daemon=True,
        )
        self._checkpoint_thread.start()

    def _checkpoint_worker(self) -> None:
        while not self._checkpoint_stop.wait(self._checkpoint_interval_seconds):
            try:
                with self._db_lock:
                    self._sqlite.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception as exc:
                logger.warning("SQLite WAL checkpoint failed: %s", exc)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _connect_to_nats(self) -> None:
        future = asyncio.run_coroutine_threadsafe(self._connect_to_nats_async(), self._loop)
        try:
            future.result(timeout=10)
        except Exception as exc:
            self._nats_ready = False
            logger.warning("NATS connection unavailable; using SQLite-only mode: %s", exc)

    async def _connect_to_nats_async(self) -> None:
        nats_config = self.config.get("nats", {})
        servers = [f"nats://{nats_config.get('host', 'localhost')}:{nats_config.get('port', 4222)}"]
        auth_token = os.environ.get("CEREBELLUM_NATS_TOKEN", "").strip()
        if not auth_token:
            raise RuntimeError(
                "CEREBELLUM_NATS_TOKEN is not configured; refusing to connect to NATS unauthenticated"
            )

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                self._nc = NATS()
                await self._nc.connect(
                    servers=servers,
                    connect_timeout=10,
                    max_reconnect_attempts=5,
                    token=auth_token,
                )
                self._js = self._nc.jetstream(domain=nats_config.get("jetstream_domain") or None)
                try:
                    await self._js.stream_info("CEREBELLUM_EVENTS")
                except Exception as exc:
                    # Stream discovery can fail during first bootstrap; log at debug before creating it.
                    logger.debug("JetStream stream_info failed during bootstrap: %s", exc)
                    await self._js.add_stream(
                        name="CEREBELLUM_EVENTS", subjects=["cerebellum.events.>"]
                    )
                self._nats_ready = True
                logger.info(
                    "Connected to NATS JetStream at %s (attempt %d)", servers[0], attempt + 1
                )
                return
            except Exception as exc:
                last_exc = exc
                self._nc = None
                self._js = None
                self._nats_ready = False
                delay = 2 ** attempt
                logger.warning(
                    "NATS connect attempt %d/3 failed: %s; retrying in %ds",
                    attempt + 1,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError(f"NATS connection failed after retries: {last_exc}")

    def _write_event(self, event: dict[str, Any], insert_mode: str = "") -> None:
        try:
            insert_clause = f"INSERT {insert_mode}".strip()
            with self._db_lock:
                self._sqlite.execute(
                    f"""
                    {insert_clause} INTO events (id, timestamp, type, payload, actor, context)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["id"],
                        event["timestamp"],
                        event["type"],
                        json.dumps(event["payload"]),
                        event["actor"],
                        json.dumps(event["context"]),
                    ),
                )
                self._sqlite.commit()
        except Exception as exc:
            logger.error("Failed to persist event %s: %s", event.get("id"), exc)
            raise

    async def _publish_event(self, event: dict[str, Any]) -> None:
        if not self._js:
            return

        subject = f"cerebellum.events.{event['type']}"
        try:
            await self._js.publish(subject, json.dumps(event).encode("utf-8"))
        except Exception as exc:
            logger.error("JetStream publish failed on %s: %s", subject, exc)
            raise

    def _wait_for_inflight_publishes(self, timeout_seconds: float) -> int:
        with self._inflight_lock:
            pending = [future for future in self._inflight_publishes if not future.done()]
        if not pending:
            return 0

        _, still_pending = wait(pending, timeout=timeout_seconds)
        with self._inflight_lock:
            self._inflight_publishes = [future for future in self._inflight_publishes if not future.done()]
            return len([future for future in self._inflight_publishes if future in still_pending])

    async def _subscribe(self, callback: Callable[[dict[str, Any]], Any]) -> None:
        if not self._nc:
            return

        async def _handler(msg: Msg) -> None:
            try:
                event = json.loads(msg.data.decode("utf-8"))
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.error("Event subscription callback failed: %s", exc)

        try:
            await self._nc.subscribe("cerebellum.events.>", cb=_handler)
            logger.info("Subscribed to cerebellum.events.>")
        except Exception as exc:
            logger.error("Failed to subscribe to NATS event stream: %s", exc)
            raise

    def _row_to_event(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "type": row["type"],
            "payload": json.loads(row["payload"]),
            "actor": row["actor"],
            "context": json.loads(row["context"]),
        }
