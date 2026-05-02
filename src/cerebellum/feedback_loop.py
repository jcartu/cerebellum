"""Feedback loop — proposal outcomes tracking and confidence calibration.

Phase 5: Track proposal outcomes (approve/reject), compute calibration
metrics, and run weekly calibration jobs.

Key concepts:
- **proposal_outcomes**: SQLite table tracking each proposal's fate
  (approved, rejected, expired) with confidence, model, and outcome.
- **Calibration**: Compare predicted confidence vs actual outcomes.
  Well-calibrated models have confidence ≈ approval rate in each bin.
- **Platt scaling**: Simple logistic regression to recalibrate confidence
  scores when calibration is poor (ECE > 0.1).
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ProposalOutcome:
    """Record of a proposal's outcome."""

    hypothesis_id: str
    model: str
    confidence: float
    outcome: str  # "approved", "rejected", "expired"
    outcome_at: str  # ISO timestamp
    reason: str = ""


@dataclass
class CalibrationMetrics:
    """Calibration metrics for a model over a time window."""

    model: str
    window_days: int
    total_outcomes: int
    approval_rate: float
    mean_confidence_approved: float
    mean_confidence_rejected: float
    expected_calibration_error: float
    is_calibrated: bool
    platt_a: float | None = None
    platt_b: float | None = None


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------


class FeedbackStore:
    """SQLite-backed store for proposal outcomes and calibration data."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._ensure_schema()
            return self._conn

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS proposal_outcomes (
                hypothesis_id TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                confidence REAL NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('approved', 'rejected', 'expired')),
                outcome_at TEXT NOT NULL,
                reason TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_proposal_outcomes_model ON proposal_outcomes(model);
            CREATE INDEX IF NOT EXISTS idx_proposal_outcomes_outcome_at ON proposal_outcomes(outcome_at);

            CREATE TABLE IF NOT EXISTS calibration_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model TEXT NOT NULL,
                window_days INTEGER NOT NULL,
                total_outcomes INTEGER NOT NULL,
                approval_rate REAL NOT NULL,
                mean_confidence_approved REAL NOT NULL,
                mean_confidence_rejected REAL NOT NULL,
                expected_calibration_error REAL NOT NULL,
                is_calibrated INTEGER NOT NULL,
                platt_a REAL,
                platt_b REAL,
                snapshot_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_calibration_snapshots_model ON calibration_snapshots(model);
            """
        )
        conn.commit()

    def record_outcome(self, outcome: ProposalOutcome) -> None:
        """Record a proposal outcome (upsert by hypothesis_id)."""
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO proposal_outcomes
                (hypothesis_id, model, confidence, outcome, outcome_at, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.hypothesis_id,
                outcome.model,
                outcome.confidence,
                outcome.outcome,
                outcome.outcome_at,
                outcome.reason,
            ),
        )
        conn.commit()

    def query_outcomes(
        self,
        model: str | None = None,
        outcome: str | None = None,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Query outcomes with optional filters."""
        conn = self._get_conn()
        query = "SELECT * FROM proposal_outcomes WHERE 1=1"
        params: list[Any] = []

        if model:
            query += " AND model = ?"
            params.append(model)
        if outcome:
            query += " AND outcome = ?"
            params.append(outcome)
        if since:
            query += " AND outcome_at >= ?"
            params.append(since.isoformat())

        query += " ORDER BY outcome_at DESC LIMIT ?"
        params.append(limit)

        cursor = conn.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def compute_calibration(
        self,
        model: str | None = None,
        window_days: int = 7,
        num_bins: int = 10,
    ) -> CalibrationMetrics:
        """Compute calibration metrics for a model over a time window.

        Uses Expected Calibration Error (ECE) with equal-width bins.
        A model is calibrated if ECE < 0.1.
        """
        conn = self._get_conn()
        since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()

        query = """
            SELECT model, confidence, outcome
            FROM proposal_outcomes
            WHERE outcome_at >= ?
            AND outcome IN ('approved', 'rejected')
        """
        params: list[Any] = [since]

        if model:
            query += " AND model = ?"
            params.append(model)

        cursor = conn.execute(query, params)
        rows = cursor.fetchall()

        if not rows:
            model_name = model or "all"
            return CalibrationMetrics(
                model=model_name,
                window_days=window_days,
                total_outcomes=0,
                approval_rate=0.0,
                mean_confidence_approved=0.0,
                mean_confidence_rejected=0.0,
                expected_calibration_error=0.0,
                is_calibrated=True,
            )

        # Group by model if none specified
        models: dict[str, list[tuple[float, str]]] = {}
        for row_model, confidence, outcome in rows:
            models.setdefault(row_model, []).append((confidence, outcome))

        # Compute for first/only model
        target_model = model or next(iter(models.keys()))
        data = models.get(target_model, [])

        total = len(data)
        approved = [c for c, o in data if o == "approved"]
        rejected = [c for c, o in data if o == "rejected"]

        approval_rate = len(approved) / total if total > 0 else 0.0
        mean_conf_approved = sum(approved) / len(approved) if approved else 0.0
        mean_conf_rejected = sum(rejected) / len(rejected) if rejected else 0.0

        # ECE: expected calibration error using equal-width bins
        ece = self._compute_ece(data, num_bins)
        is_calibrated = ece < 0.1

        # Platt scaling if not calibrated
        platt_a: float | None = None
        platt_b: float | None = None
        if not is_calibrated and total >= 10:
            platt_a, platt_b = self._fit_platt(data)

        return CalibrationMetrics(
            model=target_model,
            window_days=window_days,
            total_outcomes=total,
            approval_rate=round(approval_rate, 4),
            mean_confidence_approved=round(mean_conf_approved, 4),
            mean_confidence_rejected=round(mean_conf_rejected, 4),
            expected_calibration_error=round(ece, 4),
            is_calibrated=is_calibrated,
            platt_a=round(platt_a, 6) if platt_a is not None else None,
            platt_b=round(platt_b, 6) if platt_b is not None else None,
        )

    def _compute_ece(
        self, data: list[tuple[float, str]], num_bins: int
    ) -> float:
        """Expected Calibration Error with equal-width bins."""
        if not data:
            return 0.0

        bin_counts = [0] * num_bins
        bin_confidence_sum = [0.0] * num_bins
        bin_accuracy_sum = [0.0] * num_bins

        for confidence, outcome in data:
            bin_idx = min(int(confidence * num_bins), num_bins - 1)
            bin_counts[bin_idx] += 1
            bin_confidence_sum[bin_idx] += confidence
            bin_accuracy_sum[bin_idx] += 1 if outcome == "approved" else 0

        total = len(data)
        ece = 0.0
        for i in range(num_bins):
            if bin_counts[i] == 0:
                continue
            avg_confidence = bin_confidence_sum[i] / bin_counts[i]
            avg_accuracy = bin_accuracy_sum[i] / bin_counts[i]
            ece += (bin_counts[i] / total) * abs(avg_accuracy - avg_confidence)

        return ece

    def _fit_platt(self, data: list[tuple[float, str]]) -> tuple[float, float]:
        """Fit Platt scaling (logistic regression) via gradient descent.

        Returns (a, b) such that calibrated_p = sigmoid(a * confidence + b).
        """
        # Labels: 1 for approved, 0 for rejected
        xs = [c for c, _ in data]
        ys = [1.0 if o == "approved" else 0.0 for _, o in data]

        # Initialize
        a, b = 0.0, 0.0
        lr = 0.01
        n = len(xs)

        for _ in range(200):
            grad_a = 0.0
            grad_b = 0.0
            for x, y in zip(xs, ys, strict=True):
                p = self._sigmoid(a * x + b)
                error = p - y
                grad_a += error * x
                grad_b += error
            a -= lr * grad_a / n
            b -= lr * grad_b / n

        return a, b

    @staticmethod
    def _sigmoid(x: float) -> float:
        if x >= 0:
            return 1.0 / (1.0 + math.exp(-x))
        else:
            ex = math.exp(x)
            return ex / (1.0 + ex)

    def save_calibration_snapshot(self, metrics: CalibrationMetrics) -> None:
        """Save a calibration snapshot for historical tracking."""
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO calibration_snapshots
                (model, window_days, total_outcomes, approval_rate,
                 mean_confidence_approved, mean_confidence_rejected,
                 expected_calibration_error, is_calibrated, platt_a, platt_b)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metrics.model,
                metrics.window_days,
                metrics.total_outcomes,
                metrics.approval_rate,
                metrics.mean_confidence_approved,
                metrics.mean_confidence_rejected,
                metrics.expected_calibration_error,
                1 if metrics.is_calibrated else 0,
                metrics.platt_a,
                metrics.platt_b,
            ),
        )
        conn.commit()

    def calibrate_confidence(self, confidence: float, platt_a: float, platt_b: float) -> float:
        """Apply Platt scaling to recalibrate a confidence score."""
        return self._sigmoid(platt_a * confidence + platt_b)
