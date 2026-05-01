#!/usr/bin/env python3
import importlib.util
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = BASE_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from arbiter import BasalGanglia  # noqa: E402

LOGGER = logging.getLogger("cerebellum.arbiter_loop")
STOP_REQUESTED = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    LOGGER.info("Received signal %s; exiting after current cycle", signum)
    STOP_REQUESTED = True


def _load_cortex() -> Any:
    cortex_path = SRC_DIR / "cortex.py"
    if not cortex_path.exists():
        LOGGER.warning("cortex.py not found; using fallback queue only")
        return None
    spec = importlib.util.spec_from_file_location("cerebellum_cortex", cortex_path)
    if spec is None or spec.loader is None:
        LOGGER.warning("Could not load cortex module spec")
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cortex_cls = getattr(module, "PrefrontalCortex", None)
    if cortex_cls is None:
        LOGGER.warning("PrefrontalCortex not found in cortex.py")
        return None
    try:
        return cortex_cls(str(BASE_DIR / "config.json"))
    except Exception:
        LOGGER.exception("Failed to instantiate PrefrontalCortex")
        return None


def _query_proposed_hypotheses(cortex: Any, base_dir: Path) -> list[dict[str, Any]]:
    if cortex is not None:
        for method_name in (
            "list_hypotheses",
            "get_hypotheses",
            "fetch_hypotheses",
            "get_active_hypotheses",
            "get_proposed_hypotheses",
        ):
            method = getattr(cortex, method_name, None)
            if callable(method):
                try:
                    result = method(state="proposed")
                except TypeError:
                    try:
                        result = method(status="proposed")
                    except TypeError:
                        try:
                            result = method("proposed")
                        except TypeError:
                            result = method()
                    return [item for item in result if isinstance(item, dict) and item.get("state", item.get("status", "proposed")) == "proposed"]
    queue_file = base_dir / "graph" / "proposed_hypotheses.json"
    if queue_file.exists():
        import json

        try:
            payload = json.loads(queue_file.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
        except Exception:
            LOGGER.exception("Failed to read fallback proposed hypotheses queue")
    return []


def _load_emitter() -> Any:
    events_path = SRC_DIR / "events.py"
    if not events_path.exists():
        LOGGER.warning("events.py not found; emitter disabled")
        return None
    try:
        spec = importlib.util.spec_from_file_location("cerebellum_events", events_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        emitter_cls = getattr(module, "CerebellumEventEmitter", None)
        if emitter_cls is None:
            LOGGER.warning("CerebellumEventEmitter not found")
            return None
        return emitter_cls(str(BASE_DIR / "config.json"))
    except Exception:
        LOGGER.exception("Failed to instantiate emitter")
        return None


def _load_hippocampus() -> Any:
    hippo_path = SRC_DIR / "hippocampus.py"
    if not hippo_path.exists():
        LOGGER.warning("hippocampus.py not found; hippocampus disabled")
        return None
    try:
        spec = importlib.util.spec_from_file_location("cerebellum_hippocampus", hippo_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        hippo_cls = getattr(module, "Hippocampus", None)
        if hippo_cls is None:
            LOGGER.warning("Hippocampus not found")
            return None
        hippo = hippo_cls(str(BASE_DIR / "config.json"))
        return hippo
    except Exception:
        LOGGER.exception("Failed to instantiate hippocampus")
        return None


def _wire_cortex(cortex: Any, emitter: Any, hippocampus: Any) -> None:
    if cortex is None:
        return
    if emitter is not None and not getattr(cortex, "emitter", None):
        cortex.emitter = emitter
    if hippocampus is not None and not getattr(cortex, "hippocampus", None):
        cortex.hippocampus = hippocampus
    LOGGER.info("Cortex wired with emitter=%s, hippocampus=%s", emitter is not None, hippocampus is not None)



def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

cortex = _load_cortex()
emitter = _load_emitter()
    hippocampus = _load_hippocampus()
    _wire_cortex(cortex, emitter, hippocampus)
arbiter = BasalGanglia(str(BASE_DIR / "policy.yaml"), cortex=cortex, emitter=emitter)

    LOGGER.info("Cerebellum arbiter loop started")
    while not STOP_REQUESTED:
        cycle_started = time.time()
        try:
            hypotheses = _query_proposed_hypotheses(cortex, BASE_DIR)
            LOGGER.info("Found %d proposed hypotheses", len(hypotheses))
            for hypothesis in hypotheses:
                decision = arbiter.evaluate(hypothesis)
                if decision.decision == "auto_execute":
                    arbiter.auto_execute(hypothesis)
                elif decision.decision == "stage_notify":
                    arbiter.stage_for_approval(hypothesis)
        except Exception:
            LOGGER.exception("Arbiter cycle failed")

        if STOP_REQUESTED:
            break

        sleep_for = max(0.0, 300 - (time.time() - cycle_started))
        LOGGER.info("Cycle complete; sleeping %.1f seconds", sleep_for)
        time.sleep(sleep_for)

    LOGGER.info("Cerebellum arbiter loop stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
