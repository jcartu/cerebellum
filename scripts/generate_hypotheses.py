#!/usr/bin/env python3
"""Hypothesis generation cron script — wires emitter + hippocampus for full context."""
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = BASE_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cortex import PrefrontalCortex  # noqa: E402

logger = logging.getLogger("cerebellum.scripts.generate_hypotheses")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    try:
        config_path = str(BASE_DIR / "config.json")

        # Wire emitter for event context
        emitter = None
        try:
            from events import CerebellumEventEmitter  # noqa: F401

            emitter = CerebellumEventEmitter(config_path)
            logger.info("Emitter loaded")
        except Exception:
            logger.warning("Emitter unavailable (hypotheses will lack recent event context)")

        # Wire hippocampus for episode context
        hippocampus = None
        try:
            from hippocampus import Hippocampus  # noqa: F401

            hippocampus = Hippocampus(config_path)
            logger.info("Hippocampus loaded")
        except Exception:
            logger.warning("Hippocampus unavailable (hypotheses will lack episode context)")

        cortex = PrefrontalCortex(
            config_path=config_path,
            emitter=emitter,
            hippocampus=hippocampus,
        )
        generated = cortex.generate_hypotheses()
        expired = cortex.expire_old_hypotheses()
        logger.info("Hypothesis cycle complete: generated=%s expired=%s", len(generated), expired)

        if emitter:
            emitter.close()
        return 0
    except Exception as exc:
        logger.exception("Hypothesis generation cycle failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
