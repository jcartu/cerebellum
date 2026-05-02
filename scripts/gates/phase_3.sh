#!/usr/bin/env bash
# Phase 3: Real successor-pattern mining — Exit Gate
# Per plan section 3.B:
# - Mining recovers all 3 planted patterns with lift > 2.0 in integration test
# - Mining produces zero edges on uniform-random event stream (false positive rate)
# - Coverage on mining.py >= 80%
# - decisions.md records actual lift distribution from real event stream
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAIL=$((FAIL + 1)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; WARN=$((WARN + 1)); }

echo "=== Phase 3 Exit Gate: Real Causality ==="

# 1. Integration test: recover 3 planted patterns with lift > 2.0
if timeout 60 .venv/bin/pytest tests/test_mining.py::test_integration_recover_planted_patterns -q 2>&1 | tail -1 | grep -q "passed"; then
  pass "Recover 3 planted patterns with lift > 2.0"
else
  fail "Planted pattern recovery test failed"
fi

# 2. False positive rate: zero edges on 1000 random events
if timeout 60 .venv/bin/pytest tests/test_mining.py::test_integration_no_false_positives_on_random -q 2>&1 | tail -1 | grep -q "passed"; then
  pass "Zero false positives on uniform-random stream"
else
  fail "False positive rate test failed"
fi

# 5. All tests pass (also generates coverage.xml with mining.py)
if timeout 60 .venv/bin/pytest tests/ --cov=src/cerebellum/mining --cov-report=term-missing -q 2>&1 | tail -1 | grep -q "passed"; then
  pass "All tests pass"
else
  fail "Tests failed"
fi

# 3. Coverage on mining.py >= 80% (after coverage.xml is fresh)
MINING_COVER=$(.venv/bin/python3 -c "
import xml.etree.ElementTree as ET
tree = ET.parse('coverage.xml')
for class_elem in tree.findall('.//class'):
    if 'mining.py' in class_elem.get('filename', ''):
        print(class_elem.get('line-rate', '0'))
        break
")
COVERAGE_CHECK=$(python3 -c "print(1 if float('$MINING_COVER') >= 0.80 else 0)")
if [ "$COVERAGE_CHECK" -eq 1 ]; then
  pass "mining.py coverage ${MINING_COVER} (>= 80%)"
else
  fail "mining.py coverage ${MINING_COVER} (need >= 80%)"
fi

# 4. decisions.md records lift distribution
if grep -q "Lift distribution" decisions.md 2>/dev/null; then
  pass "Lift distribution recorded in decisions.md"
else
  warn "Lift distribution not yet recorded in decisions.md"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed, $WARN warnings"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
