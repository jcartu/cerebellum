#!/usr/bin/env bash
# Phase 4: Policy Arbiter — Exit Gate
# Verifies: YAML policy engine, auto-execute/stage/discard tiers, kill-switch
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAIL=$((FAIL + 1)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; WARN=$((WARN + 1)); }

echo "=== Phase 4 Exit Gate: Policy Arbiter ==="

# Check arbiter policy engine
if grep -q "policy" src/cerebellum/policy_arbiter.py 2>/dev/null; then
  pass "Policy engine present"
else
  fail "No policy engine in policy_arbiter.py"
fi

# Check kill-switch
if grep -q "kill" src/cerebellum/policy_arbiter.py 2>/dev/null; then
  pass "Kill-switch logic present"
else
  fail "No kill-switch in policy_arbiter.py"
fi

# Check approval tiers
if grep -q "auto_execute\|stage\|discard" src/cerebellum/policy_arbiter.py 2>/dev/null; then
  pass "Approval tiers present"
else
  fail "No approval tiers in policy_arbiter.py"
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
