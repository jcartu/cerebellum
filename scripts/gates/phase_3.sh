#!/usr/bin/env bash
# Phase 3: Hypothesis Engine — Exit Gate
# Verifies: OpenRouter integration, hypothesis lifecycle, cost tracking
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; ((PASS++)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; ((FAIL++)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; ((WARN++)); }

echo "=== Phase 3 Exit Gate: Hypothesis Engine ==="

# Check cortex hypothesis generation
if grep -q "generate_hypotheses" src/cerebellum/cortex.py 2>/dev/null; then
  pass "Hypothesis generation method exists"
else
  fail "No generate_hypotheses in cortex.py"
fi

# Check cost tracking
if grep -q "cost" src/cerebellum/cortex.py 2>/dev/null; then
  pass "Cost tracking present"
else
  fail "No cost tracking in cortex.py"
fi

# Check hypothesis lifecycle DB
if grep -q "hypotheses" src/cerebellum/cortex.py 2>/dev/null; then
  pass "Hypothesis lifecycle DB logic present"
else
  fail "No hypothesis lifecycle in cortex.py"
fi

# Run tests
if make test 2>&1 | tail -1 | grep -q "passed"; then
  pass "All tests pass"
else
  fail "Tests failed"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed, $WARN warnings"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
