"""Sequential pattern mining with lift scoring and shuffle baselines.

Replaces the naive event-type co-occurrence counter in EpisodeStore with
PrefixSpan-based mining over (entity, event_type) sequences.

Key concepts:
- **Item**: (entity, event_type) pair, e.g. ("service=api", "cron.error")
- **Sequence**: Ordered list of items within a time window
- **Pattern**: Frequent subsequence of items (length 2-4)
- **Lift**: confidence(A→B) / support(B). Lift > 1 means A→B is more frequent
  than chance. We reject edges with lift < 1.5.
- **Shuffle baseline**: Randomly shuffle timestamps, re-mine, compare lift.
  Edges where actual_lift / shuffle_lift < 2.0 are low_confidence.
"""

from __future__ import annotations

import hashlib
import logging
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """A single (entity, event_type) observation."""

    entity: str
    event_type: str

    def __str__(self) -> str:
        if self.entity:
            return f"{self.entity}/{self.event_type}"
        return self.event_type


@dataclass
class SuccessorPattern:
    """A mined sequential pattern with lift scoring."""

    id: str
    source: Item
    target: Item
    support: int
    confidence: float
    lift: float
    shuffle_baseline_lift: float | None = None
    low_confidence: bool = False
    first_seen: str = ""
    last_seen: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# PrefixSpan implementation
# ---------------------------------------------------------------------------


def _prefixspan(
    sequences: list[list[Item]],
    min_support: int,
    min_length: int = 2,
    max_length: int = 4,
) -> list[tuple[list[Item], int]]:
    """Mine frequent sequential patterns using PrefixSpan.

    Args:
        sequences: List of item sequences (each sequence is ordered in time).
        min_support: Minimum number of sequences a pattern must appear in.
        min_length: Minimum pattern length.
        max_length: Maximum pattern length.

    Returns:
        List of (pattern, support_count) tuples.
    """
    if not sequences:
        return []

    results: list[tuple[list[Item], int]] = []

    # Count single-item frequencies
    item_counts: Counter[Item] = Counter()
    for seq in sequences:
        seen = set()
        for item in seq:
            if item not in seen:
                item_counts[item] += 1
                seen.add(item)

    # Filter to frequent items
    frequent_items = {item for item, count in item_counts.items() if count >= min_support}
    if not frequent_items:
        return []

    def _project(prefix: list[Item]) -> list[list[Item]]:
        """Project database on prefix — return suffix sequences."""
        projected: list[list[Item]] = []
        for seq in sequences:
            suffix = _find_suffix(seq, prefix)
            if suffix is not None:
                projected.append(suffix)
        return projected

    def _find_suffix(seq: list[Item], prefix: list[Item]) -> list[Item] | None:
        """Find the suffix of seq after matching prefix as a subsequence."""
        prefix_idx = 0
        for i, item in enumerate(seq):
            if prefix_idx < len(prefix) and item == prefix[prefix_idx]:
                prefix_idx += 1
            if prefix_idx == len(prefix):
                return seq[i + 1 :]
        return None

    def _mine(prefix: list[Item]) -> None:
        """Recursively extend prefix."""
        projected = _project(prefix)
        proj_count = len(projected)

        if proj_count < min_support:
            return

        if len(prefix) >= min_length:
            results.append((list(prefix), proj_count))

        if len(prefix) >= max_length:
            return

        # Count frequent items in projected database
        local_counts: Counter[Item] = Counter()
        for seq in projected:
            seen = set()
            for item in seq:
                if item not in seen:
                    local_counts[item] += 1
                    seen.add(item)

        for item, count in local_counts.items():
            if count >= min_support:
                _mine([*prefix, item])

    # Seed with each frequent item
    for item in frequent_items:
        _mine([item])

    return results


# ---------------------------------------------------------------------------
# Lift computation
# ---------------------------------------------------------------------------


def compute_lift(
    pattern_support: int,
    source_support: int,
    target_support: int,
    total_events: int,
) -> float:
    """Compute lift for a pattern.

    lift = confidence(source→target) / support(target)
    confidence = pattern_support / source_support
    support(target) = target_support / total_events

    Args:
        pattern_support: Number of times the full pattern appears.
        source_support: Number of times the source item appears.
        target_support: Number of times the target item appears.
        total_events: Total number of events in the dataset.

    Returns:
        Lift score. > 1 means more frequent than chance.
    """
    if source_support == 0 or total_events == 0:
        return 0.0
    confidence = pattern_support / source_support
    target_prob = target_support / total_events
    if target_prob == 0:
        return 0.0
    return confidence / target_prob


# ---------------------------------------------------------------------------
# Main mining pipeline
# ---------------------------------------------------------------------------


def build_item_sequences(
    events: list[dict[str, Any]],
    window_hours: float = 1.0,
) -> list[list[Item]]:
    """Build item sequences from events within time windows.

    Each event is converted to an (entity, event_type) Item. Events are
    grouped into sequences where consecutive events are within `window_hours`.

    Args:
        events: List of event dicts with 'timestamp', 'type', and optional
            'entities' (list of entity names).
        window_hours: Maximum time gap between consecutive events in a sequence.

    Returns:
        List of item sequences.
    """
    if not events:
        return []

    sorted_events = sorted(events, key=lambda e: str(e.get("timestamp", "")))
    sequences: list[list[Item]] = []
    current_seq: list[Item] = []
    current_ts: list[datetime] = []
    window_delta = timedelta(hours=window_hours)

    for _i, event in enumerate(sorted_events):
        try:
            ts = _parse_timestamp(str(event.get("timestamp", "")))
        except (ValueError, TypeError):
            continue

        # Extract entities from event
        entities = event.get("entities") or []
        if isinstance(entities, list):
            entity_names = [str(e.get("name", "")) for e in entities if e.get("name")]
        elif isinstance(entities, str):
            entity_names = [e.strip() for e in entities.split("|") if e.strip()]
        else:
            entity_names = []

        # Also check common payload fields for entity-like values
        payload = event.get("payload") or {}
        if isinstance(payload, dict):
            for key in ("service", "host", "actor", "user", "repo", "project"):
                val = str(payload.get(key, "")).strip()
                if val and val not in entity_names:
                    entity_names.append(val)

        event_type = str(event.get("type", "unknown"))

        # Check if we need to start a new sequence
        if current_ts and (ts - current_ts[-1]) > window_delta:
            if len(current_seq) >= 2:
                sequences.append(current_seq)
            current_seq = []
            current_ts = []

        # Create item(s) — one per entity, plus a generic one
        if entity_names:
            for entity in entity_names:
                current_seq.append(Item(entity=entity, event_type=event_type))
                current_ts.append(ts)
        else:
            current_seq.append(Item(entity="", event_type=event_type))
            current_ts.append(ts)

    if len(current_seq) >= 2:
        sequences.append(current_seq)

    return sequences

def _parse_timestamp(ts: str) -> datetime:
    """Parse ISO timestamp, falling back to now."""
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return datetime.now(UTC)


def mine_patterns(
    events: list[dict[str, Any]],
    min_support: int = 5,
    min_length: int = 2,
    max_length: int = 4,
    window_hours: float = 1.0,
    lift_threshold: float = 1.5,
) -> list[SuccessorPattern]:
    """Mine sequential patterns from events.

    Args:
        events: List of event dicts.
        min_support: Minimum number of sequences a pattern must appear in.
        min_length: Minimum pattern length (default 2 = pairwise).
        max_length: Maximum pattern length (default 4).
        window_hours: Time window for sequence construction.
        lift_threshold: Minimum lift to accept a pattern (default 1.5).

    Returns:
        List of SuccessorPattern objects sorted by lift descending.
    """
    sequences = build_item_sequences(events, window_hours)
    if not sequences:
        return []

    # Count individual item supports across all sequences
    item_supports: Counter[Item] = Counter()
    for seq in sequences:
        seen = set()
        for item in seq:
            if item not in seen:
                item_supports[item] += 1
                seen.add(item)

    total_events = sum(item_supports.values())

    # Mine patterns
    patterns = _prefixspan(sequences, min_support, min_length, max_length)

    # Convert to SuccessorPattern with lift
    result: list[SuccessorPattern] = []
    now = datetime.now(UTC).isoformat()

    for pattern_items, support in patterns:
        if len(pattern_items) < 2:
            continue

        # We care about pairwise: first item → last item
        source = pattern_items[0]
        target = pattern_items[-1]

        src_support = item_supports.get(source, 0)
        tgt_support = item_supports.get(target, 0)

        lift = compute_lift(support, src_support, tgt_support, total_events)

        if lift < lift_threshold:
            continue

        confidence = round(support / max(src_support, 1), 4)

        edge_str = f"{source}->{target}"
        pattern_id = f"successor:{hashlib.sha1(edge_str.encode()).hexdigest()[:16]}"

        result.append(
            SuccessorPattern(
                id=pattern_id,
                source=source,
                target=target,
                support=support,
                confidence=confidence,
                lift=round(lift, 4),
                first_seen=now,
                last_seen=now,
            )
        )

    # Sort by lift descending
    result.sort(key=lambda p: p.lift, reverse=True)
    return result


# ---------------------------------------------------------------------------
# Shuffle baseline
# ---------------------------------------------------------------------------


def compute_shuffle_baseline(
    events: list[dict[str, Any]],
    num_shuffles: int = 10,
    seed: int | None = None,
    **kwargs: Any,
) -> dict[str, float]:
    """Compute lift distribution under shuffled timestamps.

    Args:
        events: Original event list.
        num_shuffles: Number of random shuffles.
        seed: Random seed for reproducibility.
        **kwargs: Passed to mine_patterns.

    Returns:
        Dict mapping pattern_id → average shuffle lift.
    """
    rng = random.Random(seed)
    shuffle_lifts: defaultdict[str, list[float]] = defaultdict(list)

    for _ in range(num_shuffles):
        shuffled = list(events)
        rng.shuffle(shuffled)

        # Re-assign shuffled timestamps
        timestamps = sorted(
            str(e.get("timestamp", "")) for e in events if e.get("timestamp")
        )
        for i, event in enumerate(shuffled):
            if i < len(timestamps):
                event = dict(event)  # shallow copy
                event["timestamp"] = timestamps[i]
                shuffled[i] = event

        patterns = mine_patterns(shuffled, **kwargs)
        for pattern in patterns:
            shuffle_lifts[pattern.id].append(pattern.lift)

    return {
        pid: round(sum(lifts) / len(lifts), 4)
        for pid, lifts in shuffle_lifts.items()
    }


def flag_low_confidence(
    patterns: list[SuccessorPattern],
    shuffle_baseline: dict[str, float],
    ratio_threshold: float = 2.0,
) -> list[SuccessorPattern]:
    """Flag patterns whose lift isn't significantly above shuffle baseline.

    Args:
        patterns: Mined patterns.
        shuffle_baseline: Pattern ID → average shuffle lift.
        ratio_threshold: actual_lift / shuffle_lift must exceed this.

    Returns:
        Patterns with low_confidence set appropriately.
    """
    for pattern in patterns:
        shuffle_lift = shuffle_baseline.get(pattern.id, 0.0)
        pattern.shuffle_baseline_lift = shuffle_lift
        if shuffle_lift > 0 and (pattern.lift / shuffle_lift) < ratio_threshold:
            pattern.low_confidence = True
    return patterns


# ---------------------------------------------------------------------------
# Pattern storage helpers
# ---------------------------------------------------------------------------


def get_relevant_patterns(
    patterns: list[SuccessorPattern],
    recent_events: list[dict[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Find patterns whose source matches recent events.

    Args:
        patterns: Mined patterns (filtered to non-low-confidence).
        recent_events: Recent events to match against.
        limit: Maximum patterns to return.

    Returns:
        Top patterns sorted by lift, limited to `limit`.
    """
    recent_types = {str(e.get("type", "")) for e in recent_events}
    recent_entities = set()
    for e in recent_events:
        entities = e.get("entities") or []
        if isinstance(entities, list):
            for ent in entities:
                if isinstance(ent, dict):
                    recent_entities.add(str(ent.get("name", "")))
                else:
                    recent_entities.add(str(ent))
        payload = e.get("payload") or {}
        if isinstance(payload, dict):
            for key in ("service", "host", "actor", "user"):
                val = str(payload.get(key, "")).strip()
                if val:
                    recent_entities.add(val)

    # Filter to high-confidence patterns matching recent context
    relevant = []
    for p in patterns:
        if p.low_confidence:
            continue
        # Match by event type or entity
        source_type_match = p.source.event_type in recent_types
        source_entity_match = p.source.entity in recent_entities if p.source.entity else False
        if source_type_match or source_entity_match:
            relevant.append(p.to_dict())

    relevant.sort(key=lambda d: d.get("lift", 0), reverse=True)
    return relevant[:limit]
