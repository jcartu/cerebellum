#!/usr/bin/env python3

import logging
import sys
from pathlib import Path

BASE_DIR = Path("/home/josh/.openclaw/cerebellum")
SRC_DIR = BASE_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cortex import PrefrontalCortex  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger = logging.getLogger("cerebellum.scripts.generate_hypotheses")

    try:
        cortex = PrefrontalCortex(config_path=str(BASE_DIR / "config.json"))
        generated = cortex.generate_hypotheses()
        expired = cortex.expire_old_hypotheses()
        logger.info("Hypothesis cycle complete: generated=%s expired=%s", len(generated), expired)
        return 0
    except Exception as exc:
        logger.exception("Hypothesis generation cycle failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
