#!/usr/bin/env python3
import importlib.util
import json
import logging
import random
import signal
import sys
import time
import types
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / "src" / "cerebellum"

from cerebellum.policy_arbiter import PolicyArbiter

logger = logging.getLogger(__name__)
STOP_REQUESTED = False
DEFAULT_SLEEP_SECONDS = 300.0
DEFAULT_SLEEP_JITTER_FRACTION = 0.1
MAX_PROPOSED_HYPOTHESES = 100


def _handle_signal(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    logger.info("Received signal %s; exiting after current cycle", signum)
    STOP_REQUESTED = True


def _load_module(module_path: Path, module_name: str) -> types.ModuleType | None:
    if not module_path.exists():
        logger.error("Module file not found: %s", module_path)
        return None

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        logger.error("Could not load module spec for %s", module_path)
        return None

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        logger.exception("Failed to import module from %s", module_path)
        return None
    return module


def _load_loop_config() -> dict[str, Any]:
    config_path = BASE_DIR / "config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("Arbiter loop config not found: %s", config_path)
    except json.JSONDecodeError:
        logger.exception("Arbiter loop config is invalid JSON: %s", config_path)
    except OSError:
        logger.exception("Failed to read arbiter loop config: %s", config_path)
    return {}


def _load_proposer() -> Any:
    proposer_path = SRC_DIR / "proposer.py"
    module = _load_module(proposer_path, "cerebellum_proposer")
    if module is None:
        logger.warning("proposer.py unavailable; using fallback queue only")
        return None

    proposer_cls = getattr(module, "Proposer", None)
    if proposer_cls is None:
        logger.warning("Proposer not found in %s", proposer_path)
        return None

    try:
        return proposer_cls(str(BASE_DIR / "config.json"))
    except Exception:
        logger.exception("Failed to instantiate Proposer from %s", proposer_path)
        return None


def _coerce_proposed_hypotheses(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict) and item.get("state", item.get("status", "proposed")) == "proposed"]


def _read_proposed_queue(queue_file: Path) -> list[dict[str, Any]]:
    if not queue_file.exists():
        return []

    try:
        payload = json.loads(queue_file.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read fallback proposed hypotheses queue from %s", queue_file)
        return []

    if not isinstance(payload, list):
        return []

    entries = [item for item in payload if isinstance(item, dict)]
    if len(entries) > MAX_PROPOSED_HYPOTHESES:
        entries = entries[-MAX_PROPOSED_HYPOTHESES:]
        try:
            queue_file.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            # Best-effort queue compaction only; continue using the in-memory trimmed payload.
            logger.debug("Failed to compact proposed hypotheses queue %s", queue_file, exc_info=True)
    return entries


def _invoke_hypothesis_method(method_name: str, method: Any) -> list[dict[str, Any]]:
    try:
        if method_name == "get_active_hypotheses":
            return _coerce_proposed_hypotheses(method(state="proposed"))
        return _coerce_proposed_hypotheses(method())
    except TypeError:
        if method_name == "get_active_hypotheses":
            logger.debug("Falling back to %s() after incompatible state= signature", method_name, exc_info=True)
            return _coerce_proposed_hypotheses(method())
        logger.debug("Skipping proposer method %s due to incompatible signature", method_name, exc_info=True)
    except Exception:
        logger.exception("Failed to query hypotheses via proposer method %s", method_name)
    return []


def _query_proposed_hypotheses(proposer: Any, base_dir: Path) -> list[dict[str, Any]]:
    if proposer is not None:
        for method_name in (
            "get_active_hypotheses",
            "get_proposed_hypotheses",
            "list_hypotheses",
            "get_hypotheses",
            "fetch_hypotheses",
        ):
            method = getattr(proposer, method_name, None)
            if callable(method):
                hypotheses = _invoke_hypothesis_method(method_name, method)
                if hypotheses:
                    return hypotheses

    queue_file = base_dir / "graph" / "proposed_hypotheses.json"
    return _read_proposed_queue(queue_file)


def _load_emitter() -> Any:
    events_path = SRC_DIR / "event_bus.py"
    module = _load_module(events_path, "cerebellum_event_bus")
    if module is None:
        logger.warning("event_bus.py unavailable; emitter disabled")
        return None

    emitter_cls = getattr(module, "EventBus", None)
    if emitter_cls is None:
        logger.warning("EventBus not found in %s", events_path)
        return None

    try:
        return emitter_cls(str(BASE_DIR / "config.json"))
    except Exception:
        logger.exception("Failed to instantiate emitter from %s", events_path)
        return None


def _load_episode_store() -> Any:
    episode_store_path = SRC_DIR / "episode_store.py"
    module = _load_module(episode_store_path, "cerebellum_episode_store")
    if module is None:
        logger.warning("episode_store.py unavailable; episode store disabled")
        return None

    episode_store_cls = getattr(module, "EpisodeStore", None)
    if episode_store_cls is None:
        logger.warning("EpisodeStore not found in %s", episode_store_path)
        return None

    try:
        return episode_store_cls(str(BASE_DIR / "config.json"))
    except Exception:
        logger.exception("Failed to instantiate episode store from %s", episode_store_path)
        return None


def _wire_proposer(proposer: Any, emitter: Any, episode_store: Any) -> None:
    if proposer is None:
        return
    if emitter is not None and not getattr(proposer, "emitter", None):
        proposer.emitter = emitter
    if episode_store is not None and not getattr(proposer, "hippocampus", None):
        proposer.hippocampus = episode_store
    logger.info("Proposer wired with emitter=%s, episode_store=%s", emitter is not None, episode_store is not None)


def _compute_sleep_duration(config: dict[str, Any], cycle_started: float) -> float:
    arbiter_loop_cfg = config.get("arbiter_loop") if isinstance(config.get("arbiter_loop"), dict) else {}
    configured_sleep = arbiter_loop_cfg.get("sleep_seconds", DEFAULT_SLEEP_SECONDS)
    configured_jitter = arbiter_loop_cfg.get("sleep_jitter_fraction", DEFAULT_SLEEP_JITTER_FRACTION)

    try:
        base_sleep_seconds = max(0.0, float(configured_sleep))
    except (TypeError, ValueError):
        logger.warning("Invalid arbiter_loop.sleep_seconds %r; using default %.1f", configured_sleep, DEFAULT_SLEEP_SECONDS)
        base_sleep_seconds = DEFAULT_SLEEP_SECONDS

    try:
        jitter_fraction = max(0.0, min(1.0, float(configured_jitter)))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid arbiter_loop.sleep_jitter_fraction %r; using default %.2f",
            configured_jitter,
            DEFAULT_SLEEP_JITTER_FRACTION,
        )
        jitter_fraction = DEFAULT_SLEEP_JITTER_FRACTION

    jitter_multiplier = random.uniform(1.0 - jitter_fraction, 1.0 + jitter_fraction)
    target_sleep = base_sleep_seconds * jitter_multiplier
    return max(0.0, target_sleep - (time.time() - cycle_started))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    config = _load_loop_config()
    proposer = _load_proposer()
    emitter = _load_emitter()
    episode_store = _load_episode_store()
    _wire_proposer(proposer, emitter, episode_store)
    arbiter = PolicyArbiter(str(BASE_DIR / "policy.yaml"), cortex=proposer, emitter=emitter)

    logger.info("Cerebellum arbiter loop started")
    while not STOP_REQUESTED:
        cycle_started = time.time()
        try:
            hypotheses = _query_proposed_hypotheses(proposer, BASE_DIR)
            logger.info("Found %d proposed hypotheses", len(hypotheses))
            for hypothesis in hypotheses:
                decision = arbiter.evaluate(hypothesis)
                if decision.decision == "auto_execute":
                    arbiter.auto_execute(hypothesis)
                elif decision.decision == "stage_notify":
                    arbiter.stage_for_approval(hypothesis)
        except Exception:
            logger.exception("Arbiter cycle failed")

        if STOP_REQUESTED:
            break

        sleep_for = _compute_sleep_duration(config, cycle_started)
        logger.info("Cycle complete; sleeping %.1f seconds", sleep_for)
        time.sleep(sleep_for)

    logger.info("Cerebellum arbiter loop stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
