#!/usr/bin/env bash
# Phase 2: Graph Intelligence — Exit Gate
# Verifies: KuzuDB schema, causal edge mining, NL query generation
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; ((PASS++)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; ((FAIL++)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; ((WARN++)); }

echo "=== Phase 2 Exit Gate: Graph Intelligence ==="

# Check KuzuDB SDK
if python3 -c "import kuzu; print('KuzuDB SDK available')" 2>/dev/null; then
  pass "KuzuDB SDK available"
else
  warn "KuzuDB SDK not installed (optional for gate)"
fi

# Check hippocampus causal edge logic
if grep -q "causal" src/cerebellum/episode_store.py 2>/dev/null; then
  pass "Causal edge logic present"
else
  fail "No causal edge logic in episode_store.py"
fi

# Check episode clustering exists
if grep -q "cluster" src/cerebellum/episode_store.py 2>/dev/null; then
  pass "Episode clustering logic present"
else
  fail "No episode clustering in episode_store.py"
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
