"""Tests for cortex_routes.py — FastAPI hypothesis routes."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture()
def config_path(tmp_path: Path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({
            "sqlite": {"events_db": str(tmp_path / "events.db")},
            "nats": {"host": "localhost", "port": 4222},
        })
    )
    return config


@pytest.fixture()
def mock_proposer():
    proposer = MagicMock()
    proposer.get_active_hypotheses.return_value = [
        {"id": "hyp-1", "state": "proposed", "title": "Test Hypothesis"},
    ]
    proposer.get_hypothesis_stats.return_value = {"total": 1, "proposed": 1}
    proposer.get_hypothesis.return_value = {"id": "hyp-1", "state": "proposed", "title": "Test"}
    proposer.update_hypothesis_state.return_value = {"id": "hyp-1", "state": "staged"}
    return proposer


@pytest.fixture()
def client(mock_proposer, config_path, monkeypatch):
    import cerebellum.ui.cortex_routes as routes
    monkeypatch.setenv("CEREBELLUM_BASE_DIR", str(config_path.parent))
    with patch.object(routes, "Proposer", return_value=mock_proposer):
        # Reset singleton
        routes._cortex = None
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(routes.router)
        yield TestClient(app)
        routes._cortex = None


class TestHealthz:
    def test_healthz_returns_ok(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestListHypotheses:
    def test_list_default(self, client, mock_proposer):
        resp = client.get("/api/hypotheses")
        assert resp.status_code == 200
        data = resp.json()
        assert "hypotheses" in data
        assert len(data["hypotheses"]) == 1
        mock_proposer.get_active_hypotheses.assert_called_once()

    def test_list_with_state_filter(self, client, mock_proposer):
        client.get("/api/hypotheses?state=proposed")
        call_kwargs = mock_proposer.get_active_hypotheses.call_args
        assert call_kwargs[1].get("state") == "proposed"

    def test_list_with_limit(self, client, mock_proposer):
        client.get("/api/hypotheses?limit=10")
        call_kwargs = mock_proposer.get_active_hypotheses.call_args
        assert call_kwargs[1].get("limit") == 10

    def test_list_error(self, client, mock_proposer):
        mock_proposer.get_active_hypotheses.side_effect = RuntimeError("db error")
        resp = client.get("/api/hypotheses")
        assert resp.status_code == 500
        assert "Failed to list hypotheses" in resp.json()["detail"]


class TestHypothesisStats:
    def test_stats_success(self, client, mock_proposer):
        resp = client.get("/api/hypotheses/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        mock_proposer.get_hypothesis_stats.assert_called_once()

    def test_stats_error(self, client, mock_proposer):
        mock_proposer.get_hypothesis_stats.side_effect = RuntimeError("db error")
        resp = client.get("/api/hypotheses/stats")
        assert resp.status_code == 500
        assert "Failed to load hypothesis stats" in resp.json()["detail"]


class TestGetHypothesis:
    def test_get_success(self, client, mock_proposer):
        resp = client.get("/api/hypotheses/hyp-1")
        assert resp.status_code == 200
        assert resp.json()["id"] == "hyp-1"

    def test_get_not_found(self, client, mock_proposer):
        mock_proposer.get_hypothesis.return_value = None
        resp = client.get("/api/hypotheses/hyp-missing")
        assert resp.status_code == 404
        assert "Hypothesis not found" in resp.json()["detail"]

    def test_get_error(self, client, mock_proposer):
        mock_proposer.get_hypothesis.side_effect = RuntimeError("db error")
        resp = client.get("/api/hypotheses/hyp-1")
        assert resp.status_code == 500
        assert "Failed to load hypothesis" in resp.json()["detail"]


class TestApproveHypothesis:
    def test_approve_success(self, client, mock_proposer):
        resp = client.post("/api/hypotheses/hyp-1/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_proposer.update_hypothesis_state.assert_called_with(
            "hyp-1", "staged", reason="approved_via_dashboard"
        )

    def test_approve_not_found(self, client, mock_proposer):
        mock_proposer.update_hypothesis_state.return_value = None
        resp = client.post("/api/hypotheses/hyp-missing/approve")
        assert resp.status_code == 404
        assert "Hypothesis not found" in resp.json()["detail"]

    def test_approve_error(self, client, mock_proposer):
        mock_proposer.update_hypothesis_state.side_effect = RuntimeError("db error")
        resp = client.post("/api/hypotheses/hyp-1/approve")
        assert resp.status_code == 500
        assert "Failed to approve hypothesis" in resp.json()["detail"]


class TestRejectHypothesis:
    def test_reject_success(self, client, mock_proposer):
        resp = client.post("/api/hypotheses/hyp-1/reject")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_proposer.update_hypothesis_state.assert_called_with(
            "hyp-1", "rejected", reason="rejected_via_dashboard"
        )

    def test_reject_not_found(self, client, mock_proposer):
        mock_proposer.update_hypothesis_state.return_value = None
        resp = client.post("/api/hypotheses/hyp-missing/reject")
        assert resp.status_code == 404
        assert "Hypothesis not found" in resp.json()["detail"]

    def test_reject_error(self, client, mock_proposer):
        mock_proposer.update_hypothesis_state.side_effect = RuntimeError("db error")
        resp = client.post("/api/hypotheses/hyp-1/reject")
        assert resp.status_code == 500
        assert "Failed to reject hypothesis" in resp.json()["detail"]
