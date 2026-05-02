#!/usr/bin/env bash
# Phase 6: Test, harden, ship — Exit Gate
set -euo pipefail

PASS=0
FAIL=0
TOTAL=5

echo "=== Phase 6 Exit Gate: Test, Harden, Ship ==="
echo ""

# 1+2: Run tests with coverage in one shot
COVERAGE_OUTPUT=$(.venv/bin/python -m pytest tests/ --cov=cerebellum --cov-report=term -q 2>&1)
TEST_EXIT=$?

if [ $TEST_EXIT -eq 0 ]; then
    echo "  [PASS] All tests pass"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Tests failed"
    FAIL=$((FAIL + 1))
fi

# Parse coverage from output
GLOBAL_COV=$(echo "$COVERAGE_OUTPUT" | grep "^TOTAL" | awk '{print $6}' | tr -d '%')
ARBITER_COV=$(echo "$COVERAGE_OUTPUT" | grep "policy_arbiter.py" | awk '{print $6}' | tr -d '%')
DASHBOARD_COV=$(echo "$COVERAGE_OUTPUT" | grep "dashboard.py" | awk '{print $6}' | tr -d '%')

echo "  Coverage: global=${GLOBAL_COV}%, arbiter=${ARBITER_COV}%, dashboard=${DASHBOARD_COV}%"

if [ "${GLOBAL_COV:-0}" -ge 70 ]; then
    echo "  [PASS] Global coverage >= 70% (${GLOBAL_COV}%)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Global coverage < 70% (${GLOBAL_COV}%)"
    FAIL=$((FAIL + 1))
fi

# 3. mypy clean
if .venv/bin/python -m mypy src/cerebellum > /dev/null 2>&1; then
    echo "  [PASS] mypy clean"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] mypy errors found"
    FAIL=$((FAIL + 1))
fi

# 4. ruff lint clean
if .venv/bin/ruff check src/cerebellum tests/ > /dev/null 2>&1; then
    echo "  [PASS] ruff lint clean"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] ruff lint errors found"
    FAIL=$((FAIL + 1))
fi

# 5. decisions.md filled
if grep -q "## Phase 6" decisions.md 2>/dev/null; then
    echo "  [PASS] decisions.md contains Phase 6 entry"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] decisions.md missing Phase 6 entry"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: $PASS/${TOTAL} passed, $FAIL failed ==="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi

echo "Phase 6 exit gate PASSED."
exit 0
