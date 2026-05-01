from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

BASE_DIR = Path("/home/josh/.openclaw/cerebellum")

router = APIRouter(tags=["cerebellum-hypotheses"])


def _get_cortex():
    import sys

    src_dir = Path(__file__).resolve().parents[1]
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    from cortex import PrefrontalCortex

    return PrefrontalCortex(config_path=str(BASE_DIR / "config.json"))


@router.get("/api/hypotheses")
def list_hypotheses(state: Optional[str] = Query(default=None), limit: int = Query(default=20, ge=1, le=200)):
    try:
        cortex = _get_cortex()
        return {"hypotheses": cortex.get_active_hypotheses(state=state, limit=limit)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to list hypotheses: {exc}") from exc


@router.get("/api/hypotheses/stats")
def hypothesis_stats():
    try:
        cortex = _get_cortex()
        return cortex.get_hypothesis_stats()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load hypothesis stats: {exc}") from exc


@router.get("/api/hypotheses/{hypothesis_id}")
def get_hypothesis(hypothesis_id: str):
    try:
        cortex = _get_cortex()
        hypothesis = cortex.get_hypothesis(hypothesis_id)
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
        cortex = _get_cortex()
        updated = cortex.update_hypothesis_state(hypothesis_id, "staged", reason="approved_via_dashboard")
        if not updated:
            raise HTTPException(status_code=404, detail="Hypothesis not found")
        hypothesis = cortex.get_hypothesis(hypothesis_id)
        return {"ok": True, "hypothesis": hypothesis}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to approve hypothesis: {exc}") from exc


@router.post("/api/hypotheses/{hypothesis_id}/reject")
def reject_hypothesis(hypothesis_id: str):
    try:
        cortex = _get_cortex()
        updated = cortex.update_hypothesis_state(hypothesis_id, "rejected", reason="rejected_via_dashboard")
        if not updated:
            raise HTTPException(status_code=404, detail="Hypothesis not found")
        hypothesis = cortex.get_hypothesis(hypothesis_id)
        return {"ok": True, "hypothesis": hypothesis}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to reject hypothesis: {exc}") from exc
