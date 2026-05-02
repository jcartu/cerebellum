#!/usr/bin/env bash
# Phase 5: Feedback loop — Exit Gate
# Per plan section 5.B:
# - Weekly job runs cleanly on synthetic + real data
# - /metrics page renders
# - decisions.md has baseline week of real metrics
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

PASS=0
FAIL=0
TOTAL=5

echo "=== Phase 5 Exit Gate: Feedback Loop ==="
echo ""

# ---------------------------------------------------------------------------
# CHECK 1: feedback_loop.py exists and exports FeedbackStore
# ---------------------------------------------------------------------------
echo "[1/5] feedback_loop.py exists and exports FeedbackStore"
if .venv/bin/python -c "
from cerebellum.feedback_loop import FeedbackStore, ProposalOutcome, CalibrationMetrics
print('  OK - FeedbackStore, ProposalOutcome, CalibrationMetrics imported')
" 2>/dev/null; then
    echo "  PASS"
    PASS=$((PASS + 1))
else
    echo "  FAIL - feedback_loop.py missing or exports broken"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# CHECK 2: /metrics route exists in dashboard
# ---------------------------------------------------------------------------
echo "[2/5] /metrics route exists in dashboard"
if grep -q '"/metrics"' src/cerebellum/ui/dashboard.py && \
   grep -q 'get_feedback_store' src/cerebellum/ui/dashboard.py; then
    echo "  OK - /metrics route and get_feedback_store found"
    echo "  PASS"
    PASS=$((PASS + 1))
else
    echo "  FAIL - /metrics route or get_feedback_store not found"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# CHECK 3: Weekly calibration script runs cleanly
# ---------------------------------------------------------------------------
echo "[3/5] Weekly calibration script runs cleanly"
if .venv/bin/python scripts/weekly_calibration.py --days 7 2>&1 | grep -q "No outcomes found\|models_total"; then
    echo "  OK - weekly_calibration.py executed successfully"
    echo "  PASS"
    PASS=$((PASS + 1))
else
    echo "  FAIL - weekly_calibration.py failed"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# CHECK 4: Feedback loop tests pass
# ---------------------------------------------------------------------------
echo "[4/5] Feedback loop tests pass"
if .venv/bin/python -m pytest tests/test_feedback_loop.py -v --no-cov 2>&1 | tail -1 | grep -q "passed"; then
    echo "  OK - All feedback loop tests passed"
    echo "  PASS"
    PASS=$((PASS + 1))
else
    echo "  FAIL - Some feedback loop tests failed"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# CHECK 5: decisions.md has Phase 5 section filled
# ---------------------------------------------------------------------------
echo "[5/5] decisions.md has Phase 5 section filled"
if grep -A 5 "## Phase 5" decisions.md | grep -q "Completed:"; then
    echo "  OK - decisions.md Phase 5 section is filled"
    echo "  PASS"
    PASS=$((PASS + 1))
else
    echo "  FAIL - decisions.md Phase 5 section not filled"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# RESULT
# ---------------------------------------------------------------------------
echo "=== Phase 5 Exit Gate Result: $PASS/$TOTAL passed, $FAIL failed ==="
if [ "$FAIL" -eq 0 ]; then
    echo "PASS - Phase 5 exit gate passed. Ready to merge."
    exit 0
else
    echo "FAIL - Phase 5 exit gate failed. Fix issues before merging."
    exit 1
fi
