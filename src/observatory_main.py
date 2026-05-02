#!/usr/bin/env python3
"""Cerebellum Observatory — single event store, NATS subscriber, no dashboard subprocess."""
import asyncio
import importlib.util
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("cerebellum.observatory")
BASE_DIR = Path(__file__).resolve().parent.parent


class ObservatoryService:
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
        LOGGER.info("Shutdown requested")
        self.stop_requested = True

    async def _start_emitter(self) -> None:
        events_module = BASE_DIR / "src" / "events.py"
        if not events_module.exists():
            LOGGER.warning("events.py missing; continuing without event emitter")
            return
        spec = importlib.util.spec_from_file_location("cerebellum_events", events_module)
        if spec is None or spec.loader is None:
            LOGGER.warning("Could not load events module spec")
            return
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        emitter_cls = getattr(module, "CerebellumEventEmitter", None)
        if emitter_cls is None:
            LOGGER.warning("CerebellumEventEmitter not found")
            return
        try:
            self._emitter = emitter_cls(str(BASE_DIR / "config.json"))
            LOGGER.info("Event emitter started (single event store: events.db)")
        except Exception:
            LOGGER.exception("Failed to start event emitter")
            self._emitter = None

    async def _run_nats_loop(self) -> None:
        if importlib.util.find_spec("nats") is None:
            LOGGER.warning("nats-py is not installed; observatory will idle")
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
                except Exception:
                    LOGGER.exception("NATS handler failed for subject %s", message.subject)

            await self._nats_client.subscribe("cerebellum.events.>", cb=handler)
            LOGGER.info("Subscribed to cerebellum.events.> on nats://127.0.0.1:4222")

            while not self.stop_requested:
                await asyncio.sleep(1)
        except Exception:
            LOGGER.exception("NATS subscription loop failed")
            while not self.stop_requested:
                await asyncio.sleep(5)
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        if self._nats_client is not None:
            try:
                await self._nats_client.drain()
            except Exception:
                LOGGER.exception("Failed to drain NATS client")
        if self._emitter is not None:
            try:
                self._emitter.close()
            except Exception:
                LOGGER.exception("Emitter shutdown failed")

    def _relay_event(self, topic: str, data: str) -> None:
        """Relay NATS events through the single emitter (→ events.db + JetStream)."""
        if self._emitter is None:
            return
        try:
            event_type = topic.replace("cerebellum.events.", "", 1)
            self._emitter.emit(
                event_type,
                payload={"subject": topic, "data": data},
                actor="observatory.nats-subscriber",
            )
        except Exception:
            LOGGER.exception("Failed to relay NATS event %s through emitter", topic)


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
        LOGGER.info("Interrupted")
        return 0
    except Exception:
        LOGGER.exception("Observatory crashed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
