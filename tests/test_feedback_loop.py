"""Tests for Phase 5: feedback_loop module."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("DASHBOARD_TOKEN", "test-token")

from cerebellum.feedback_loop import FeedbackStore, ProposalOutcome


class TestFeedbackStore:
    def setup_method(self) -> None:
        self._tmp = tempfile.mktemp(suffix=".db")
        self.store = FeedbackStore(self._tmp)

    def teardown_method(self) -> None:
        Path(self._tmp).unlink(missing_ok=True)

    def _outcome(
        self,
        hid: str = "test-1",
        model: str = "test-model",
        confidence: float = 0.8,
        outcome: str = "approved",
        delta_hours: int = 0,
    ) -> ProposalOutcome:
        ts = (datetime.now(UTC) - timedelta(hours=delta_hours)).isoformat()
        return ProposalOutcome(
            hypothesis_id=hid,
            model=model,
            confidence=confidence,
            outcome=outcome,
            outcome_at=ts,
            reason="test",
        )

    def test_record_and_query(self) -> None:
        self.store.record_outcome(self._outcome())
        results = self.store.query_outcomes()
        assert len(results) == 1
        assert results[0]["hypothesis_id"] == "test-1"
        assert results[0]["outcome"] == "approved"

    def test_record_upsert(self) -> None:
        self.store.record_outcome(self._outcome(outcome="approved"))
        self.store.record_outcome(self._outcome(outcome="rejected"))
        results = self.store.query_outcomes()
        assert len(results) == 1
        assert results[0]["outcome"] == "rejected"

    def test_query_filter_by_model(self) -> None:
        self.store.record_outcome(self._outcome(model="model-a"))
        self.store.record_outcome(self._outcome(hid="test-2", model="model-b"))
        assert len(self.store.query_outcomes(model="model-a")) == 1
        assert len(self.store.query_outcomes(model="model-b")) == 1

    def test_query_filter_by_outcome(self) -> None:
        self.store.record_outcome(self._outcome(outcome="approved"))
        self.store.record_outcome(self._outcome(hid="test-2", outcome="rejected"))
        assert len(self.store.query_outcomes(outcome="approved")) == 1
        assert len(self.store.query_outcomes(outcome="rejected")) == 1

    def test_query_limit(self) -> None:
        for i in range(20):
            self.store.record_outcome(self._outcome(hid=f"test-{i}"))
        assert len(self.store.query_outcomes(limit=10)) == 10

    def test_empty_calibration(self) -> None:
        metrics = self.store.compute_calibration()
        assert metrics.total_outcomes == 0
        assert metrics.is_calibrated is True

    def test_calibrated_model(self) -> None:
        """High confidence + approved should be well-calibrated."""
        for i in range(30):
            self.store.record_outcome(
                self._outcome(hid=f"cal-{i}", confidence=0.9, outcome="approved")
            )
        metrics = self.store.compute_calibration()
        assert metrics.approval_rate == 1.0
        # ECE is 0.1 exactly for perfect calibration at 0.9 confidence
        assert metrics.expected_calibration_error <= 0.1

    def test_uncalibrated_model(self) -> None:
        """High confidence + rejected should be uncalibrated."""
        for i in range(30):
            self.store.record_outcome(
                self._outcome(hid=f"uncal-{i}", confidence=0.95, outcome="rejected")
            )
        metrics = self.store.compute_calibration()
        assert metrics.total_outcomes == 30
        assert metrics.approval_rate == 0.0
        assert metrics.is_calibrated is False
        assert metrics.platt_a is not None
        assert metrics.platt_b is not None

    def test_platt_scaling_applied(self) -> None:
        """Platt scaling should recalibrate extreme confidence."""
        for i in range(20):
            self.store.record_outcome(
                self._outcome(hid=f"platt-{i}", confidence=0.95, outcome="rejected")
            )
        metrics = self.store.compute_calibration()
        assert metrics.platt_a is not None
        assert metrics.platt_b is not None
        # Recalibrate a high confidence
        calibrated = self.store.calibrate_confidence(0.95, metrics.platt_a, metrics.platt_b)
        assert calibrated < 0.95

    def test_save_calibration_snapshot(self) -> None:
        metrics = self.store.compute_calibration()
        self.store.save_calibration_snapshot(metrics)
        # Just verify no exception


class TestSigmoid:
    def test_sigmoid_zero(self) -> None:
        assert FeedbackStore._sigmoid(0.0) == 0.5

    def test_sigmoid_large_positive(self) -> None:
        assert FeedbackStore._sigmoid(10.0) > 0.99

    def test_sigmoid_large_negative(self) -> None:
        assert FeedbackStore._sigmoid(-10.0) < 0.01


class TestProposalOutcome:
    def test_create(self) -> None:
        o = ProposalOutcome(
            hypothesis_id="h-1",
            model="gpt-4o",
            confidence=0.85,
            outcome="approved",
            outcome_at=datetime.now(UTC).isoformat(),
        )
        assert o.hypothesis_id == "h-1"
        assert o.outcome == "approved"
