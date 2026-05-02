#!/usr/bin/env python3
from __future__ import annotations

import logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

from cerebellum.episode_store import EpisodeStore

logger = logging.getLogger(__name__)
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    try:
        episode_store = EpisodeStore(str(BASE_DIR / "config.json"))
        edges = episode_store.mine_successor_edges(window_hours=168)
        if not edges:
            logger.info("No successor edges discovered in the last week")
            return 0

        for edge in edges:
            logger.info(
                "Discovered successor edge %s -> %s (support=%s confidence=%s)",
                edge["source_type"],
                edge["target_type"],
                edge["support"],
                edge["confidence"],
            )
        return 0
    except Exception as exc:
        logger.error("Successor edge mining failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
