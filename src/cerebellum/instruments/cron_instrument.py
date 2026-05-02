from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from cerebellum.events import CerebellumEventEmitter


class CronInstrumenter:
    def __init__(self, emitter: CerebellumEventEmitter) -> None:
        self.emitter = emitter

    def run(self, job_name: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        started_at = time.perf_counter()
        self.emitter.emit(
            "cron.start",
            {"job_name": job_name},
            actor="cron",
        )

        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.emitter.emit(
                "cron.error",
                {
                    "job_name": job_name,
                    "duration_ms": duration_ms,
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                },
                actor="cron",
            )
            raise

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        self.emitter.emit(
            "cron.end",
            {"job_name": job_name, "duration_ms": duration_ms},
            actor="cron",
        )
        return result
