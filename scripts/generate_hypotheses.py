#!/usr/bin/env python3
"""Hypothesis generation cron script — wires emitter + hippocampus for full context."""
import logging
from pathlib import Path

from cerebellum.cortex import PrefrontalCortex

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    try:
        config_path = str(Path(__file__).resolve().parents[1] / "config.json")

        # Wire emitter for event context
        emitter = None
        try:
            from cerebellum.events import CerebellumEventEmitter

            emitter = CerebellumEventEmitter(config_path)
            logger.info("Emitter loaded")
        except Exception:
            # Optional dependency wiring may fail in reduced environments; continue with degraded context.
            logger.debug("Emitter wiring skipped", exc_info=True)
            logger.warning("Emitter unavailable (hypotheses will lack recent event context)")

        # Wire hippocampus for episode context
        hippocampus = None
        try:
            from cerebellum.hippocampus import Hippocampus

            hippocampus = Hippocampus(config_path)
            logger.info("Hippocampus loaded")
        except Exception:
            # Optional dependency wiring may fail in reduced environments; continue with degraded context.
            logger.debug("Hippocampus wiring skipped", exc_info=True)
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
