import asyncio
import importlib.util
import json
import logging
import signal
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("cerebellum.observatory")
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "graph" / "observatory.sqlite3"


class ObservatoryService:
    def __init__(self) -> None:
        self.stop_requested = False
        self._emitter: Any = None
        self._dashboard_task: asyncio.Task[Any] | None = None
        self._nats_client: Any = None

    async def run(self) -> None:
        self._ensure_db()
        self._install_signal_handlers()
        await self._start_emitter()
        await self._start_dashboard()
        await self._run_nats_loop()

    def _ensure_db(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

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
            start_method = getattr(self._emitter, "start", None)
            if callable(start_method):
                result = start_method()
                if asyncio.iscoroutine(result):
                    await result
            LOGGER.info("Event emitter started")
        except Exception:
            LOGGER.exception("Failed to start event emitter")
            self._emitter = None

    async def _start_dashboard(self) -> None:
        dashboard_module = BASE_DIR / "src" / "ui" / "dashboard.py"
        if not dashboard_module.exists():
            LOGGER.warning("Dashboard module missing; continuing without dashboard")
            return
        try:
            import subprocess

            self._dashboard_proc = subprocess.Popen(
                [sys.executable, str(dashboard_module)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            LOGGER.info("Dashboard started as subprocess (pid=%d)", self._dashboard_proc.pid)
        except Exception:
            LOGGER.exception("Dashboard startup failed")
            self._dashboard_proc = None

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
                payload = {
                    "subject": message.subject,
                    "reply": message.reply,
                    "data": message.data.decode("utf-8", errors="replace"),
                }
                self._write_event(message.subject, payload)

            await self._nats_client.subscribe(">", cb=handler)
            LOGGER.info("Subscribed to NATS events on nats://127.0.0.1:4222")

            while not self.stop_requested:
                await asyncio.sleep(1)
        except Exception:
            LOGGER.exception("NATS subscription loop failed")
            while not self.stop_requested:
                await asyncio.sleep(5)
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        if hasattr(self, '_dashboard_proc') and self._dashboard_proc is not None:
            try:
                self._dashboard_proc.terminate()
                self._dashboard_proc.wait(timeout=5)
            except Exception:
                LOGGER.exception("Failed to terminate dashboard subprocess")
        if self._nats_client is not None:
            try:
                await self._nats_client.drain()
            except Exception:
                LOGGER.exception("Failed to drain NATS client")
        if self._emitter is not None:
            stop_method = getattr(self._emitter, "stop", None)
            if callable(stop_method):
                try:
                    result = stop_method()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    LOGGER.exception("Emitter shutdown failed")
    def _write_event(self, topic: str, payload: dict[str, Any]) -> None:
        with sqlite3.connect(DB_PATH) as connection:
            connection.execute(
                "INSERT INTO events(topic, payload, created_at) VALUES (?, ?, ?)",
                (topic, json.dumps(payload), datetime.utcnow().isoformat()),
            )
            connection.commit()


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
