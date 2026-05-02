#!/usr/bin/env bash
# Phase 2: Grounding — Exit Gate
# Verifies: grounding module, evidence fields, proposal caps, behavioral tests
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAIL=$((FAIL + 1)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; WARN=$((WARN + 1)); }

echo "=== Phase 2 Exit Gate: Grounding ==="

# Check grounding module exists
if [ -f src/cerebellum/grounding.py ]; then
  pass "grounding.py exists"
else
  fail "grounding.py missing"
fi

# Check GroundingVerifier class
if grep -q "class GroundingVerifier" src/cerebellum/grounding.py 2>/dev/null; then
  pass "GroundingVerifier class present"
else
  fail "No GroundingVerifier class"
fi

# Check evidence_event_ids in Hypothesis
if grep -q "evidence_event_ids" src/cerebellum/proposer.py 2>/dev/null; then
  pass "evidence_event_ids field present in proposer"
else
  fail "No evidence_event_ids in proposer"
fi

# Check causal_argument in Hypothesis
if grep -q "causal_argument" src/cerebellum/proposer.py 2>/dev/null; then
  pass "causal_argument field present in proposer"
else
  fail "No causal_argument in proposer"
fi

# Check proposal cap logic
if grep -q "_proposal_cap" src/cerebellum/proposer.py 2>/dev/null; then
  pass "Proposal volume cap present"
else
  fail "No proposal volume cap"
fi

# Check duplicate detection
if grep -q "_is_duplicate" src/cerebellum/proposer.py 2>/dev/null; then
  pass "Evidence-overlap duplicate detection present"
else
  fail "No duplicate detection"
fi

# Check grounding prompt rules
if grep -q "evidence_event_ids must reference" src/cerebellum/proposer.py 2>/dev/null; then
  pass "Grounding rules in LLM prompt"
else
  fail "No grounding rules in prompt"
fi

# Run linter
if .venv/bin/ruff check src/cerebellum/grounding.py src/cerebellum/proposer.py 2>&1 | grep -q "All checks passed"; then
  pass "Lint clean (grounding.py + proposer.py)"
else
  fail "Lint errors found"
fi

# Run tests with coverage
COVERAGE_OUTPUT=$(timeout 120 .venv/bin/pytest tests/test_proposer.py tests/test_grounding.py tests/test_episode_store.py tests/test_event_bus.py tests/test_imports.py --cov=src/cerebellum/proposer --cov=src/cerebellum/grounding --cov-report=term-missing -q 2>&1)

if echo "$COVERAGE_OUTPUT" | grep -q "passed"; then
  pass "All tests pass"
else
  fail "Tests failed"
fi

# Check proposer coverage >= 70%
PROPOSER_COVER=$(echo "$COVERAGE_OUTPUT" | grep "src/cerebellum/proposer.py" | awk '{for(i=1;i<=NF;i++) if($i ~ /%/) {gsub(/%/,"",$i); print $i}}')
if [ -n "$PROPOSER_COVER" ] && [ "$PROPOSER_COVER" -ge 70 ] 2>/dev/null; then
  pass "proposer.py coverage ${PROPOSER_COVER}% (>= 70%)"
else
  fail "proposer.py coverage ${PROPOSER_COVER:-0}% (need >= 70%)"
fi

# Check grounding coverage >= 70%
GROUNDING_COVER=$(echo "$COVERAGE_OUTPUT" | grep "src/cerebellum/grounding.py" | awk '{for(i=1;i<=NF;i++) if($i ~ /%/) {gsub(/%/,"",$i); print $i}}')
if [ -n "$GROUNDING_COVER" ] && [ "$GROUNDING_COVER" -ge 70 ] 2>/dev/null; then
  pass "grounding.py coverage ${GROUNDING_COVER}% (>= 70%)"
else
  fail "grounding.py coverage ${GROUNDING_COVER:-0}% (need >= 70%)"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed, $WARN warnings"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
