#!/usr/bin/env python3
from __future__ import annotations

import logging
import sys
from pathlib import Path


BASE_DIR = Path("/home/josh/.openclaw")
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from cerebellum.src.hippocampus import Hippocampus  # noqa: E402


logger = logging.getLogger("cerebellum.mine_causal_edges")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    try:
        hippocampus = Hippocampus("/home/josh/.openclaw/cerebellum/config.json")
        edges = hippocampus.mine_causal_edges(window_hours=168)
        if not edges:
            logger.info("No causal edges discovered in the last week")
            return 0

        for edge in edges:
            logger.info(
                "Discovered causal edge %s -> %s (support=%s confidence=%s)",
                edge["source_type"],
                edge["target_type"],
                edge["support"],
                edge["confidence"],
            )
        return 0
    except Exception as exc:
        logger.error("Causal edge mining failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
