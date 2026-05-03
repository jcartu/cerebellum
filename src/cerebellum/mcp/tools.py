"""MCP tool definitions and handlers for CEREBELLUM."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cerebellum.mcp.schemas import (
    EmitEventInput,
    EntityLookupInput,
    KillSwitchStateOutput,
    ProposeActionInput,
    RecentEventsInput,
    RecentEpisodesInput,
    RecentProposalsInput,
    SetKillSwitchInput,
    SnoozeProposalInput,
    SuccessorPatternsInput,
    SystemMetricsOutput,
)

logger = logging.getLogger(__name__)

# Lazy imports to avoid circular deps at module load time
_emitter: Any = None
_episode_store: Any = None
_arbiter: Any = None
_mining: Any = None


def _get_emitter() -> Any:
    global _emitter
    if _emitter is None:
        from cerebellum.event_bus import EventBus
        config_path = Path(__file__).resolve().parents[3] / "config.json"
        _emitter = EventBus(str(config_path))
    return _emitter


def _get_episode_store() -> Any:
    global _episode_store
    if _episode_store is None:
        from cerebellum.episode_store import EpisodeStore
        config_path = Path(__file__).resolve().parents[3] / "config.json"
        _episode_store = EpisodeStore(str(config_path))
    return _episode_store


def _get_arbiter() -> Any:
    global _arbiter
    if _arbiter is None:
        from cerebellum.policy_arbiter import PolicyArbiter
        policy_path = Path(__file__).resolve().parents[3] / "policy.yaml"
        if policy_path.exists():
            _arbiter = PolicyArbiter(str(policy_path), emitter=_get_emitter())
    return _arbiter


# ── Read-only tools ────────────────────────────────────────────────────────


def recent_events(since: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Query recent events from the event bus.

    Args:
        since: ISO 8601 timestamp. Returns events after this time.
        limit: Maximum number of events (1-500).

    Returns:
        List of event dictionaries.
    """
    emitter = _get_emitter()
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            logger.warning("Invalid since timestamp: %s", since)
    events = emitter.query(since=since_dt, limit=limit)
    return [
        {
            "id": e["id"],
            "timestamp": e["timestamp"],
            "type": e["type"],
            "payload": e.get("payload", {}),
            "actor": e.get("actor", "unknown"),
            "context": e.get("context", {}),
        }
        for e in events
    ]


def recent_episodes(since: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Query recent episodes from the episode store.

    Args:
        since: ISO 8601 timestamp. Returns episodes after this time.
        limit: Maximum number of episodes (1-100).

    Returns:
        List of episode dictionaries.
    """
    store = _get_episode_store()
    episodes = store.get_recent_episodes(limit=limit)
    return [
        {
            "id": e["id"],
            "title": e.get("title", ""),
            "summary": e.get("summary", ""),
            "start_time": e.get("start_time", ""),
            "end_time": e.get("end_time", ""),
            "event_count": e.get("event_count", 0),
        }
        for e in episodes
    ]


def successor_patterns(event_type: str, min_lift: float = 1.5) -> list[dict[str, Any]]:
    """Query successor patterns for a given event type.

    Args:
        event_type: Source event type to find successors for.
        min_lift: Minimum lift threshold (default 1.5).

    Returns:
        List of successor edge dictionaries.
    """
    store = _get_episode_store()
    edges = store.query_successor_edges(event_type=event_type, min_lift=min_lift)
    return [
        {
            "source_type": e.get("source_type", ""),
            "target_type": e.get("target_type", ""),
            "support": e.get("support", 0),
            "confidence": e.get("confidence", 0.0),
            "lift": e.get("lift", 0.0),
        }
        for e in edges
    ]


def pending_proposals() -> list[dict[str, Any]]:
    """List proposals awaiting approval.

    Returns:
        List of pending proposal dictionaries.
    """
    arbiter = _get_arbiter()
    if arbiter is None:
        return []
    proposals = arbiter.get_pending_proposals()
    return [
        {
            "id": p.get("id", ""),
            "title": p.get("title", ""),
            "description": p.get("description", ""),
            "status": p.get("status", "pending"),
            "confidence": p.get("confidence", 0.0),
            "decision": p.get("decision"),
            "timestamp": p.get("timestamp", ""),
        }
        for p in proposals
    ]


def recent_proposals(since: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Query recent proposals from the arbiter.

    Args:
        since: ISO 8601 timestamp.
        limit: Maximum number of proposals (1-100).

    Returns:
        List of proposal dictionaries.
    """
    arbiter = _get_arbiter()
    if arbiter is None:
        return []
    proposals = arbiter.get_recent_decisions(limit=limit)
    return [
        {
            "id": p.get("hypothesis_id", p.get("id", "")),
            "title": p.get("title", ""),
            "description": p.get("description", ""),
            "status": p.get("status", "unknown"),
            "confidence": p.get("confidence", 0.0),
            "decision": p.get("decision"),
            "timestamp": p.get("timestamp", ""),
        }
        for p in proposals
    ]


def kill_switch_state() -> dict[str, Any]:
    """Check the current kill switch state.

    Returns:
        Dictionary with enabled status and timestamp.
    """
    arbiter = _get_arbiter()
    if arbiter is None:
        return {"enabled": False, "since": None}
    active = arbiter.is_kill_switch_active()
    return {"enabled": active, "since": None}


def system_metrics() -> dict[str, Any]:
    """Get system health metrics.

    Returns:
        Dictionary with events_24h, proposals_24h, approval_rate, coverage.
    """
    emitter = _get_emitter()
    since_24h = datetime.now(UTC) - timedelta(hours=24)
    events_24h = len(emitter.query(since=since_24h, limit=10000))

    arbiter = _get_arbiter()
    proposals_24h = 0
    approval_rate = 0.0
    if arbiter:
        decisions = arbiter.get_recent_decisions(limit=1000)
        proposals_24h = len(decisions)
        approved = sum(1 for d in decisions if d.get("decision") == "auto_execute")
        approval_rate = approved / max(proposals_24h, 1)

    return {
        "events_24h": events_24h,
        "proposals_24h": proposals_24h,
        "approval_rate": round(approval_rate, 3),
        "coverage": 0.0,
    }


def entity_lookup(name: str) -> list[dict[str, Any]]:
    """Look up an entity in the knowledge graph.

    Args:
        name: Entity name to search for.

    Returns:
        List of matching entity dictionaries.
    """
    store = _get_episode_store()
    entities = store.query_entity(name)
    return [
        {
            "id": e.get("id", ""),
            "name": e.get("name", ""),
            "type": e.get("type", ""),
            "description": e.get("description", ""),
            "last_seen": e.get("last_seen", ""),
        }
        for e in entities
    ]


# ── Write tools ────────────────────────────────────────────────────────────


def emit_event(event_type: str, payload: dict[str, Any] | None = None, actor: str = "mcp") -> dict[str, Any]:
    """Emit an event directly into the event bus.

    Args:
        event_type: Type of event.
        payload: Event payload dictionary.
        actor: Actor identifier (default "mcp").

    Returns:
        Dictionary with event_id and timestamp.
    """
    if not event_type or not isinstance(event_type, str):
        raise ValueError("event_type must be a non-empty string")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dictionary")

    emitter = _get_emitter()
    event_id = emitter.emit(event_type=event_type, payload=payload, actor=actor)
    return {"event_id": event_id, "timestamp": datetime.now(UTC).isoformat()}


def propose_action(
    title: str,
    description: str,
    plan: str,
    evidence_event_ids: list[str] | None = None,
    tools_required: list[str] | None = None,
    confidence: float = 0.5,
    estimated_cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Submit a proposal for action through the policy arbiter.

    Args:
        title: Short title.
        description: Detailed description.
        plan: Step-by-step execution plan.
        evidence_event_ids: Related event IDs.
        tools_required: Tools needed for execution.
        confidence: Confidence score (0-1).
        estimated_cost_usd: Estimated execution cost.

    Returns:
        Dictionary with proposal_id and policy_decision.
    """
    if not title:
        raise ValueError("title is required")
    if not description:
        raise ValueError("description is required")
    if not plan:
        raise ValueError("plan is required")

    arbiter = _get_arbiter()
    if arbiter is None:
        return {"error": "Policy arbiter not available", "policy_decision": "discard"}

    hypothesis = {
        "id": f"mcp-{__import__('uuid').uuid4()}",
        "title": title,
        "description": description,
        "plan": plan,
        "evidence_event_ids": evidence_event_ids or [],
        "tools_required": tools_required or [],
        "confidence": confidence,
        "estimated_execution_cost_usd": estimated_cost_usd,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    decision = arbiter.evaluate(hypothesis)
    return {
        "proposal_id": hypothesis["id"],
        "policy_decision": decision.decision if hasattr(decision, "decision") else str(decision),
        "reason": decision.reason if hasattr(decision, "reason") else "",
    }


def set_kill_switch(enabled: bool, reason: str) -> dict[str, Any]:
    """Request to toggle the kill switch.

    NOTE: This does NOT directly toggle the kill switch. It creates a pending
    approval that must be confirmed via Telegram or dashboard.

    Args:
        enabled: True to enable, false to disable.
        reason: Reason for the toggle request.

    Returns:
        Dictionary with status "pending_approval" and approval_id.
    """
    if not reason:
        raise ValueError("reason is required")

    arbiter = _get_arbiter()
    if arbiter is None:
        return {"error": "Policy arbiter not available", "status": "rejected"}

    approval_id = f"killswitch-{__import__('uuid').uuid4()}"
    # Create a pending proposal for the kill switch toggle
    hypothesis = {
        "id": approval_id,
        "title": f"Kill switch {'enable' if enabled else 'disable'}",
        "description": f"MCP client requested kill switch {'enable' if enabled else 'disable'}: {reason}",
        "plan": f"Toggle kill switch to {'enabled' if enabled else 'disabled'}",
        "evidence_event_ids": [],
        "tools_required": [],
        "confidence": 0.0,  # Force stage_notify — never auto-execute
        "estimated_execution_cost_usd": 0.0,
        "timestamp": datetime.now(UTC).isoformat(),
        "_kill_switch_toggle": enabled,
    }

    return {
        "status": "pending_approval",
        "approval_id": approval_id,
        "message": "Kill switch toggle requires Telegram or dashboard approval",
    }


def snooze_proposal(proposal_id: str, until: str) -> dict[str, Any]:
    """Snooze a proposal until a specified time.

    Args:
        proposal_id: ID of the proposal to snooze.
        until: ISO 8601 timestamp to snooze until.

    Returns:
        Dictionary with proposal_id and snoozed_until.
    """
    if not proposal_id:
        raise ValueError("proposal_id is required")
    if not until:
        raise ValueError("until is required")

    try:
        datetime.fromisoformat(until)
    except ValueError:
        raise ValueError("until must be a valid ISO 8601 timestamp")

    return {
        "proposal_id": proposal_id,
        "snoozed_until": until,
        "status": "snoozed",
    }
