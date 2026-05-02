#!/usr/bin/env python3
"""Hypothesis generation cron script — wires event bus + episode store for full context."""
import logging
from pathlib import Path

from cerebellum.proposer import Proposer

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
            from cerebellum.event_bus import EventBus

            emitter = EventBus(config_path)
            logger.info("Emitter loaded")
        except Exception:
            # Optional dependency wiring may fail in reduced environments; continue with degraded context.
            logger.debug("Emitter wiring skipped", exc_info=True)
            logger.warning("Emitter unavailable (hypotheses will lack recent event context)")

        # Wire episode store for episode context
        episode_store = None
        try:
            from cerebellum.episode_store import EpisodeStore

            episode_store = EpisodeStore(config_path)
            logger.info("EpisodeStore loaded")
        except Exception:
            # Optional dependency wiring may fail in reduced environments; continue with degraded context.
            logger.debug("EpisodeStore wiring skipped", exc_info=True)
            logger.warning("EpisodeStore unavailable (hypotheses will lack episode context)")

        proposer = Proposer(
            config_path=config_path,
            emitter=emitter,
            hippocampus=episode_store,
        )
        generated = proposer.generate_hypotheses()
        expired = proposer.expire_old_hypotheses()
        logger.info("Hypothesis cycle complete: generated=%s expired=%s", len(generated), expired)

        if emitter:
            emitter.close()
        return 0
    except Exception as exc:
        logger.exception("Hypothesis generation cycle failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
