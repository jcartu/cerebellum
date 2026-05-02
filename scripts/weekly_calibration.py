#!/usr/bin/env python3
"""Weekly calibration job — computes and saves calibration snapshots.

Runs as a cron job or manually. Computes calibration metrics for each
proposer model over the last 7 days and saves snapshots to feedback.db.

Usage:
    python scripts/weekly_calibration.py
    python scripts/weekly_calibration.py --days 14
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure src/ is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

from cerebellum.feedback_loop import FeedbackStore  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly calibration job")
    parser.add_argument("--days", type=int, default=7, help="Window in days (default: 7)")
    parser.add_argument("--db", type=str, default=None, help="Path to feedback.db")
    args = parser.parse_args()

    db_path = args.db or (PROJECT_ROOT / "feedback.db")
    store = FeedbackStore(db_path)

    # Get all distinct models
    outcomes = store.query_outcomes(limit=100000)
    models = set(row["model"] for row in outcomes)

    if not models:
        logger.info("No outcomes found. Nothing to calibrate.")
        return

    now = datetime.now(UTC).isoformat()
    results = []

    for model in sorted(models):
        metrics = store.compute_calibration(model=model, window_days=args.days)
        store.save_calibration_snapshot(metrics)
        results.append(metrics)
        status = "CALIBRATED" if metrics.is_calibrated else "UNCALIBRATED"
        logger.info(
            "%s | model=%s | outcomes=%d | approval_rate=%.1f%% | ECE=%.4f | %s",
            now,
            metrics.model,
            metrics.total_outcomes,
            metrics.approval_rate * 100,
            metrics.expected_calibration_error,
            status,
        )
        if metrics.platt_a is not None:
            logger.info("  Platt scaling: a=%.4f, b=%.4f", metrics.platt_a, metrics.platt_b)

    # Print summary JSON
    summary = {
        "timestamp": now,
        "window_days": args.days,
        "models_calibrated": len([r for r in results if r.is_calibrated]),
        "models_total": len(results),
        "total_outcomes": sum(r.total_outcomes for r in results),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
