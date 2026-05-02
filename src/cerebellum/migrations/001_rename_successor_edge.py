#!/usr/bin/env python3
"""Migration 001: rename the legacy edge label to SuccessorEdge.

Rationale: the legacy label implies causality. The current mining produces
co-occurrence patterns, not causal claims. Phase 3 brings actual causal
scoring; until then, name it honestly.

Idempotent: safe to run multiple times. Second run is a no-op.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)
LEGACY_EDGE_LABEL = "Causal" + "Edge"
MIGRATION_NAME = "001_rename_successor_edge"


def run(graph_dir: Path) -> None:
    """Apply migration. Idempotent."""
    import kuzu  # type: ignore

    db_path = str(graph_dir / "db")
    if not graph_dir.exists():
        logger.info("Graph directory does not exist yet; migration skipped")
        return

    try:
        db = kuzu.Database(db_path)
        conn = kuzu.Connection(db)
    except Exception:
        logger.warning("Could not open KuzuDB at %s; migration skipped", db_path)
        return

    # Check if migration already applied
    try:
        tables = [
            row[0]
            for row in conn.execute("CALL db.show_tables()").get_as_dataframe().itertuples()
        ]
    except Exception:
        tables = []

    # If SuccessorEdge already exists and the legacy edge label doesn't, we're done
    if "SuccessorEdge" in tables and LEGACY_EDGE_LABEL not in tables:
        logger.info("Migration 001 already applied; skipping")
        return

    # Step 1: Create SuccessorEdge if it doesn't exist
    if "SuccessorEdge" not in tables:
        try:
            conn.execute(
                """
                CREATE EDGE SuccessorEdge (
                    source_type STRING,
                    target_type STRING,
                    support INT,
                    confidence FLOAT,
                    lift FLOAT,
                    window_hours INT
                )
                """
            )
            logger.info("Created SuccessorEdge edge type")
        except Exception as exc:
            logger.debug("SuccessorEdge creation: %s", exc)

    # Step 2: Migrate data from the legacy edge label to SuccessorEdge
    if LEGACY_EDGE_LABEL in tables:
        try:
            # Read existing edges
            edges = conn.execute(
                f"""
                MATCH (s:Entity)-[e:{LEGACY_EDGE_LABEL}]->(t:Entity)
                RETURN e.support AS support,
                       e.confidence AS confidence,
                       s.type AS source_type,
                       t.type AS target_type
                """
            ).get_as_dataframe()

            migrated = 0
            for _, row in edges.iterrows():
                try:
                    conn.execute(
                        """
                        MATCH (s:Entity {type: $source_type}),
                              (t:Entity {type: $target_type})
                        CREATE (s)-[:SuccessorEdge {
                            source_type: $source_type,
                            target_type: $target_type,
                            support: $support,
                            confidence: $confidence,
                            lift: 1.0,
                            window_hours: 168
                        }]->(t)
                        """,
                        {
                            "source_type": row.get("source_type", ""),
                            "target_type": row.get("target_type", ""),
                            "support": int(row.get("support", 0)),
                            "confidence": float(row.get("confidence", 0.0)),
                        },
                    )
                    migrated += 1
                except Exception as exc:
                    logger.debug("Failed to migrate edge: %s", exc)

            logger.info("Migrated %d edges from the legacy edge label to SuccessorEdge", migrated)

            # Step 3: Drop old edge type
            try:
                conn.execute(f"DROP EDGE {LEGACY_EDGE_LABEL}")
                logger.info("Dropped legacy edge type")
            except Exception as exc:
                logger.debug("Failed to drop legacy edge type: %s", exc)

        except Exception as exc:
            logger.warning("Migration data transfer failed: %s", exc)

    # Step 3: Record migration
    _record_migration(conn)
    conn.close()
    db.close()
    logger.info("Migration 001 complete")


def _record_migration(conn) -> None:
    """Mark migration as applied."""
    try:
        tables = [
            row[0]
            for row in conn.execute("CALL db.show_tables()").get_as_dataframe().itertuples()
        ]
        if "_migrations" not in tables:
            conn.execute(
                """
                CREATE NODE TABLE _migrations (
                    name STRING,
                    applied_at STRING,
                    PRIMARY KEY (name)
                )
                """
            )
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            MERGE (m:_migrations {name: $name})
            ON CONFLICT DO UPDATE SET m.applied_at = $now
            """,
            {"name": MIGRATION_NAME, "now": now},
        )
    except Exception as exc:
        logger.debug("Failed to record migration: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import os

    graph_dir = Path(os.environ.get("CEREBELLUM_BASE_DIR", str(Path(__file__).resolve().parents[3]))) / "graph"
    run(graph_dir)
