from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

BASE_DIR = Path("/home/josh/.openclaw/cerebellum")
src_dir = Path(__file__).resolve().parents[1]
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

router = APIRouter(tags=["cerebellum-hypotheses"])

# Singleton cortex — avoids per-request instantiation (H10 fix)
_cortex: Any = None


def _get_cortex():
    global _cortex
    if _cortex is None:
        from cortex import PrefrontalCortex

        _cortex = PrefrontalCortex(config_path=str(BASE_DIR / "config.json"))
    return _cortex


@router.get("/healthz")
def healthz():
    return {"status": "ok"}


@router.get("/api/hypotheses")
def list_hypotheses(state: str | None = Query(default=None), limit: int = Query(default=20, ge=1, le=200)):
    try:
        return {"hypotheses": _get_cortex().get_active_hypotheses(state=state, limit=limit)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to list hypotheses: {exc}") from exc


@router.get("/api/hypotheses/stats")
def hypothesis_stats():
    try:
        return _get_cortex().get_hypothesis_stats()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load hypothesis stats: {exc}") from exc


@router.get("/api/hypotheses/{hypothesis_id}")
def get_hypothesis(hypothesis_id: str):
    try:
        hypothesis = _get_cortex().get_hypothesis(hypothesis_id)
        if not hypothesis:
            raise HTTPException(status_code=404, detail="Hypothesis not found")
        return hypothesis
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load hypothesis: {exc}") from exc


@router.post("/api/hypotheses/{hypothesis_id}/approve")
def approve_hypothesis(hypothesis_id: str):
    try:
        updated = _get_cortex().update_hypothesis_state(hypothesis_id, "staged", reason="approved_via_dashboard")
        if not updated:
            raise HTTPException(status_code=404, detail="Hypothesis not found")
        hypothesis = _get_cortex().get_hypothesis(hypothesis_id)
        return {"ok": True, "hypothesis": hypothesis}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to approve hypothesis: {exc}") from exc


@router.post("/api/hypotheses/{hypothesis_id}/reject")
def reject_hypothesis(hypothesis_id: str):
    try:
        updated = _get_cortex().update_hypothesis_state(hypothesis_id, "rejected", reason="rejected_via_dashboard")
        if not updated:
            raise HTTPException(status_code=404, detail="Hypothesis not found")
        hypothesis = _get_cortex().get_hypothesis(hypothesis_id)
        return {"ok": True, "hypothesis": hypothesis}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to reject hypothesis: {exc}") from exc
