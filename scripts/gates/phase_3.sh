#!/usr/bin/env bash
# Phase 3: Real Causality — Exit Gate
# Verifies: PrefixSpan mining, lift scoring, shuffle baselines, entity-aware patterns
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAIL=$((FAIL + 1)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; WARN=$((WARN + 1)); }

echo "=== Phase 3 Exit Gate: Real Causality ==="

# Check mining module exists
if [ -f src/cerebellum/mining.py ]; then
  pass "mining.py exists"
else
  fail "mining.py not found"
fi

# Check PrefixSpan implementation
if grep -q "_prefixspan" src/cerebellum/mining.py 2>/dev/null; then
  pass "PrefixSpan implementation exists"
else
  fail "No PrefixSpan in mining.py"
fi

# Check lift scoring
if grep -q "compute_lift" src/cerebellum/mining.py 2>/dev/null; then
  pass "Lift scoring implementation exists"
else
  fail "No lift scoring in mining.py"
fi

# Check shuffle baselines
if grep -q "compute_shuffle_baseline" src/cerebellum/mining.py 2>/dev/null; then
  pass "Shuffle baseline implementation exists"
else
  fail "No shuffle baselines in mining.py"
fi

# Check entity-aware mining
if grep -q "build_item_sequences" src/cerebellum/mining.py 2>/dev/null; then
  pass "Entity-aware item sequence building exists"
else
  fail "No entity-aware mining in mining.py"
fi

# Check mine_successor_edges uses PrefixSpan
if grep -q "mine_patterns" src/cerebellum/episode_store.py 2>/dev/null; then
  pass "EpisodeStore uses PrefixSpan mining"
else
  fail "EpisodeStore not using PrefixSpan mining"
fi

# Check old heuristic removed
if grep -q "_event_types_change_significantly" src/cerebellum/episode_store.py 2>/dev/null; then
  fail "Old _event_types_change_significantly heuristic still present"
else
  pass "Old heuristic removed"
fi

# Check proposer surfaces patterns
if grep -q "_get_relevant_patterns" src/cerebellum/proposer.py 2>/dev/null; then
  pass "Proposer surfaces successor patterns"
else
  fail "Proposer not surfacing patterns"
fi

# Check mining tests exist
if [ -f tests/test_mining.py ]; then
  pass "Mining tests exist"
else
  fail "No mining tests found"
fi

# Check mining test coverage
MINING_COVERAGE=$(.venv/bin/python3 -c "
import xml.etree.ElementTree as ET
tree = ET.parse('coverage.xml')
for class_elem in tree.findall('.//class'):
    if 'mining.py' in class_elem.get('filename', ''):
        print(class_elem.get('line-rate', '0'))
        break
")
COVERAGE_CHECK=$(python3 -c "print(1 if float('$MINING_COVERAGE') > 0.8 else 0)")
if [ "$COVERAGE_CHECK" -eq 1 ]; then
  pass "Mining coverage >= 80% (${MINING_COVERAGE})"
else
  fail "Mining coverage < 80% (${MINING_COVERAGE})"
fi

# Check lift column in schema
if grep -q "lift FLOAT" src/cerebellum/episode_store.py 2>/dev/null; then
  pass "Lift column in SuccessorEdge schema"
else
  fail "No lift column in schema"
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
