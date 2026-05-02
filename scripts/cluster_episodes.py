#!/usr/bin/env python3
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent

from cerebellum.episode_store import EpisodeStore
from cerebellum.event_bus import EventBus

logger = logging.getLogger(__name__)

def cluster_by_time(events: list[dict[str, Any]], threshold_minutes: int = 5) -> list[list[dict[str, Any]]]:
    if not events:
        return []

    threshold = timedelta(minutes=threshold_minutes)
    ordered = sorted(events, key=lambda item: item.get("timestamp", ""))
    clusters: list[list[dict[str, Any]]] = [[ordered[0]]]

    for event in ordered[1:]:
        previous = clusters[-1][-1]
        current_dt = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00")).astimezone(UTC)
        previous_dt = datetime.fromisoformat(str(previous["timestamp"]).replace("Z", "+00:00")).astimezone(UTC)
        if current_dt - previous_dt <= threshold:
            clusters[-1].append(event)
        else:
            clusters.append([event])

    return [cluster for cluster in clusters if len(cluster) >= 2]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    config_path = str(BASE_DIR / "config.json")
    emitter: EventBus | None = None

    try:
        emitter = EventBus(config_path)
        episode_store = EpisodeStore(config_path)
        since = datetime.now(UTC) - timedelta(minutes=15)
        events = emitter.query(since=since, limit=500)
        clusters = cluster_by_time(events, threshold_minutes=5)

        if not clusters:
            logger.info("No eligible event clusters found in the last 15 minutes")
            return 0

        for cluster in clusters:
            try:
                episode_id = episode_store.create_episode(cluster)
                logger.info("Created episode %s from %d events", episode_id, len(cluster))
            except Exception as exc:
                logger.error("Failed to create episode from cluster of %d events: %s", len(cluster), exc)

        logger.info("Processed %d clusters from %d events", len(clusters), len(events))
        return 0
    except Exception as exc:
        logger.error("Episode clustering failed: %s", exc, exc_info=True)
        return 1
    finally:
        if emitter is not None:
            try:
                emitter.close()
            except Exception as exc:
                logger.warning("Failed to close event emitter cleanly: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
