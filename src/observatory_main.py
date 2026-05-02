#!/usr/bin/env python3
"""Cerebellum Observatory — single event store, NATS subscriber, no dashboard subprocess."""
import asyncio
import importlib.util
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent


class ObservatoryService:
    RELAY_ACTOR = "observatory.nats-subscriber"

    def __init__(self) -> None:
        self.stop_requested = False
        self._emitter: Any = None
        self._nats_client: Any = None

    async def run(self) -> None:
        self._install_signal_handlers()
        await self._start_emitter()
        await self._run_nats_loop()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except NotImplementedError:
                signal.signal(sig, lambda *_args: self._request_stop())

    def _request_stop(self) -> None:
        logger.info("Shutdown requested")
        self.stop_requested = True

    async def _start_emitter(self) -> None:
        events_module = BASE_DIR / "src" / "events.py"
        if not events_module.exists():
            logger.warning("events.py missing; continuing without event emitter")
            return
        spec = importlib.util.spec_from_file_location("cerebellum_events", events_module)
        if spec is None or spec.loader is None:
            logger.warning("Could not load events module spec")
            return
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        emitter_cls = getattr(module, "CerebellumEventEmitter", None)
        if emitter_cls is None:
            logger.warning("CerebellumEventEmitter not found")
            return
        try:
            self._emitter = emitter_cls(str(BASE_DIR / "config.json"))
            logger.info("Event emitter started (single event store: events.db)")
        except Exception as exc:
            logger.exception("Failed to start event emitter: %s", exc)
            self._emitter = None

    async def _run_nats_loop(self) -> None:
        if importlib.util.find_spec("nats") is None:
            logger.warning("nats-py is not installed; observatory will idle")
            while not self.stop_requested:
                await asyncio.sleep(5)
            await self._shutdown()
            return

        import nats  # type: ignore

        try:
            self._nats_client = await nats.connect("nats://127.0.0.1:4222")

            async def handler(message: Any) -> None:
                try:
                    data = message.data.decode("utf-8", errors="replace")
                    self._relay_event(message.subject, data)
                except Exception as exc:
                    logger.exception("NATS handler failed for subject %s: %s", message.subject, exc)

            await self._nats_client.subscribe("cerebellum.events.>", cb=handler)
            logger.info("Subscribed to cerebellum.events.> on nats://127.0.0.1:4222")

            while not self.stop_requested:
                await asyncio.sleep(1)
        except Exception as exc:
            logger.exception("NATS subscription loop failed: %s", exc)
            while not self.stop_requested:
                await asyncio.sleep(5)
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        if self._nats_client is not None:
            try:
                await self._nats_client.drain()
            except Exception as exc:
                logger.exception("Failed to drain NATS client: %s", exc)
        if self._emitter is not None:
            try:
                self._emitter.close()
            except Exception as exc:
                logger.exception("Emitter shutdown failed: %s", exc)

    def _relay_event(self, topic: str, data: str) -> None:
        """Relay NATS events directly into SQLite without touching private emitter state."""
        if self._emitter is None:
            return
        try:
            try:
                parsed = json.loads(data)
            except Exception as exc:
                # Relay inputs can be malformed during transport or tests; log and drop instead of crashing the subscriber.
                logger.debug("Relay dropped malformed JSON on %s: %s", topic, exc)
                parsed = None

            if not isinstance(parsed, dict):
                logger.debug("Relay dropped non-JSON payload on %s", topic)
                return

            # Guard against historical loop via relay-origin events.
            if parsed.get("actor") == self.RELAY_ACTOR:
                return

            # Validate/normalize the event shape.
            event_id = str(parsed.get("id") or "").strip()
            if not event_id:
                logger.debug("Relay dropped event without id on %s", topic)
                return
            event_type = str(parsed.get("type") or topic.replace("cerebellum.events.", "", 1))
            timestamp = str(parsed.get("timestamp") or "")
            actor = str(parsed.get("actor") or "unknown")
            payload = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else {}
            context = parsed.get("context") if isinstance(parsed.get("context"), dict) else {}
            self._emitter.write_event(
                {
                    "id": event_id,
                    "timestamp": timestamp,
                    "type": event_type,
                    "payload": payload,
                    "actor": actor,
                    "context": context,
                }
            )
        except Exception as exc:
            logger.exception("Failed to relay NATS event %s: %s", topic, exc)


async def _async_main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    service = ObservatoryService()
    await service.run()
    return 0


def main() -> int:
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_async_main())
        finally:
            loop.close()
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 0
    except Exception as exc:
        logger.exception("Observatory crashed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
