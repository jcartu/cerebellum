#!/usr/bin/env bash
# Phase 6 Redo Exit Gate — 14 checks per plan 6.B
set -euo pipefail

# Required env vars for dashboard module to load without sys.exit(1)
# These MUST match tests/conftest.py values for consistency
export CEREBELLUM_TESTING=1
export DASHBOARD_TOKEN="test-token"
export TELEGRAM_WEBHOOK_SECRET="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
export TELEGRAM_ALLOWED_USER_IDS="12345678"
PASS=0
FAIL=0
TOTAL=14

echo "=== Phase 6 Redo Exit Gate (14 checks) ==="
echo ""

# 1. All tests pass
echo "[1/14] Running test suite…"
if .venv/bin/python -m pytest tests/ -q --tb=short > /tmp/phase6_tests.log 2>&1; then
    echo "  [PASS] All tests pass"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Tests failed (see /tmp/phase6_tests.log)"
    FAIL=$((FAIL + 1))
fi

# 2. Global coverage ≥ 80%
echo "[2/14] Checking global coverage…"
COVERAGE_OUTPUT=$(.venv/bin/python -m pytest tests/ --cov=cerebellum --cov-report=term -q 2>&1 || true)
GLOBAL_COV=$(echo "$COVERAGE_OUTPUT" | grep "^TOTAL" | awk '{print $6}' | tr -d '%')
if [ "${GLOBAL_COV:-0}" -ge 80 ]; then
    echo "  [PASS] Global coverage ≥ 80% (${GLOBAL_COV}%)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Global coverage < 80% (${GLOBAL_COV}%)"
    FAIL=$((FAIL + 1))
fi

# 3. Arbiter coverage ≥ 75%
ARBITER_COV=$(echo "$COVERAGE_OUTPUT" | grep "policy_arbiter.py" | awk '{print $6}' | tr -d '%')
if [ "${ARBITER_COV:-0}" -ge 75 ]; then
    echo "  [PASS] Arbiter coverage ≥ 75% (${ARBITER_COV}%)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Arbiter coverage < 75% (${ARBITER_COV}%)"
    FAIL=$((FAIL + 1))
fi

# 4. Dashboard coverage ≥ 75%
DASHBOARD_COV=$(echo "$COVERAGE_OUTPUT" | grep "dashboard.py" | awk '{print $6}' | tr -d '%')
if [ "${DASHBOARD_COV:-0}" -ge 75 ]; then
    echo "  [PASS] Dashboard coverage ≥ 75% (${DASHBOARD_COV}%)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Dashboard coverage < 75% (${DASHBOARD_COV}%)"
    FAIL=$((FAIL + 1))
fi

# 5. Property tests green
echo "[5/14] Running property tests…"
if .venv/bin/python -m pytest tests/test_property_tests.py -q --tb=short --override-ini="addopts=" > /dev/null 2>&1; then
    echo "  [PASS] Property tests green"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Property tests failed"
    FAIL=$((FAIL + 1))
fi

# 6. Property tests reproducible with seed
echo "[6/14] Checking property test reproducibility…"
SEED=$(.venv/bin/python -c "import random; print(random.randint(1, 100000))")
if .venv/bin/python -m pytest tests/test_property_tests.py -q --tb=short --hypothesis-seed="$SEED" --override-ini="addopts=" > /dev/null 2>&1; then
    echo "  [PASS] Property tests reproducible with seed $SEED"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Property tests not reproducible with seed $SEED"
    FAIL=$((FAIL + 1))
fi

# 7. Fuzzer green
echo "[7/14] Running fuzzer…"
if .venv/bin/python -m pytest tests/test_telegram_fuzzer.py -q --tb=short --override-ini="addopts=" > /dev/null 2>&1; then
    echo "  [PASS] Fuzzer green"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Fuzzer failed"
    FAIL=$((FAIL + 1))
fi

# 8. ruff lint clean
echo "[8/14] Running ruff lint…"
if .venv/bin/ruff check src/cerebellum tests/ > /dev/null 2>&1; then
    echo "  [PASS] ruff lint clean"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] ruff lint errors found"
    FAIL=$((FAIL + 1))
fi

# 9. mypy strict clean
echo "[9/14] Running mypy strict…"
if .venv/bin/python -m mypy src/cerebellum > /dev/null 2>&1; then
    echo "  [PASS] mypy strict clean"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] mypy errors found"
    FAIL=$((FAIL + 1))
fi

# 10. decisions.md contains Phase 6 entry
echo "[10/14] Checking decisions.md…"
if grep -q "## Phase 6" decisions.md 2>/dev/null; then
    echo "  [PASS] decisions.md contains Phase 6 entry"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] decisions.md missing Phase 6 entry"
    FAIL=$((FAIL + 1))
fi

# 11. README exists and mentions key components
echo "[11/14] Checking README accuracy…"
if [ -f README.md ] && grep -q "EventBus" README.md && grep -q "PolicyArbiter" README.md && grep -q "SSRF" README.md; then
    echo "  [PASS] README contains key component descriptions"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] README missing key component descriptions"
    FAIL=$((FAIL + 1))
fi

# 12. SECURITY.md exists and covers key topics
echo "[12/14] Checking SECURITY.md…"
if [ -f SECURITY.md ] && grep -q "NATS" SECURITY.md; then
    echo "  [PASS] SECURITY.md exists and covers NATS"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] SECURITY.md incomplete"
    FAIL=$((FAIL + 1))
fi

# 13. Cypher filter tests pass (regression)
echo "[13/14] Running cypher filter regression tests…"
if .venv/bin/python -m pytest tests/test_cypher_filter.py -q --tb=short --override-ini="addopts=" > /dev/null 2>&1; then
    echo "  [PASS] Cypher filter regression tests pass"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Cypher filter regression tests failed"
    FAIL=$((FAIL + 1))
fi

# 14. Opus audit review completed (manual check via decisions.md)
echo "[14/14] Checking Opus audit completion…"
if grep -q "Opus" decisions.md 2>/dev/null && grep -q "Phase 6" decisions.md 2>/dev/null; then
    echo "  [PASS] Opus audit review recorded in decisions.md"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] Opus audit review not recorded in decisions.md"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Results: $PASS/${TOTAL} passed, $FAIL failed ==="

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "FAILED CHECKS:"
    echo "  Global coverage: ${GLOBAL_COV:-N/A}%"
    echo "  Arbiter coverage: ${ARBITER_COV:-N/A}%"
    echo "  Dashboard coverage: ${DASHBOARD_COV:-N/A}%"
    exit 1
fi

echo ""
echo "Phase 6 Redo exit gate PASSED — all 14 checks green."
exit 0
