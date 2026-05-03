"""Pydantic schemas for MCP tool inputs/outputs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Input schemas ──────────────────────────────────────────────────────────


class RecentEventsInput(BaseModel):
    since: str | None = Field(None, description="ISO 8601 timestamp. Events after this time.")
    limit: int = Field(50, ge=1, le=500, description="Maximum number of events to return.")


class RecentEpisodesInput(BaseModel):
    since: str | None = Field(None, description="ISO 8601 timestamp. Episodes after this time.")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of episodes to return.")


class SuccessorPatternsInput(BaseModel):
    event_type: str = Field(..., description="Source event type to find successors for.")
    min_lift: float = Field(1.5, ge=0.0, description="Minimum lift threshold.")


class RecentProposalsInput(BaseModel):
    since: str | None = Field(None, description="ISO 8601 timestamp.")
    limit: int = Field(20, ge=1, le=100, description="Maximum number of proposals.")


class EntityLookupInput(BaseModel):
    name: str = Field(..., description="Entity name to look up.")


class EmitEventInput(BaseModel):
    event_type: str = Field(..., description="Type of event to emit.")
    payload: dict[str, Any] = Field(default_factory=dict, description="Event payload.")
    actor: str = Field("mcp", description="Actor identifier.")


class ProposeActionInput(BaseModel):
    title: str = Field(..., description="Short title for the proposal.")
    description: str = Field(..., description="Detailed description.")
    plan: str = Field(..., description="Step-by-step execution plan.")
    evidence_event_ids: list[str] = Field(default_factory=list, description="Related event IDs.")
    tools_required: list[str] = Field(default_factory=list, description="Tools needed.")
    confidence: float = Field(0.5, ge=0.0, le=1.0, description="Confidence score.")
    estimated_cost_usd: float = Field(0.0, ge=0.0, description="Estimated execution cost.")


class SetKillSwitchInput(BaseModel):
    enabled: bool = Field(..., description="True to enable, false to disable.")
    reason: str = Field(..., description="Reason for toggling kill switch.")


class SnoozeProposalInput(BaseModel):
    proposal_id: str = Field(..., description="ID of the proposal to snooze.")
    until: str = Field(..., description="ISO 8601 timestamp to snooze until.")


# ── Output schemas ─────────────────────────────────────────────────────────


class EventOutput(BaseModel):
    id: str
    timestamp: str
    type: str
    payload: dict[str, Any]
    actor: str
    context: dict[str, Any]


class EpisodeOutput(BaseModel):
    id: str
    title: str
    summary: str
    start_time: str
    end_time: str
    event_count: int


class SuccessorEdgeOutput(BaseModel):
    source_type: str
    target_type: str
    support: int
    confidence: float
    lift: float


class ProposalOutput(BaseModel):
    id: str
    title: str
    description: str
    status: str
    confidence: float
    decision: str | None = None
    timestamp: str


class EntityOutput(BaseModel):
    id: str
    name: str
    type: str
    description: str
    last_seen: str


class KillSwitchStateOutput(BaseModel):
    enabled: bool
    since: str | None = None


class SystemMetricsOutput(BaseModel):
    events_24h: int
    proposals_24h: int
    approval_rate: float
    coverage: float
