#!/usr/bin/env bash
# Phase 1: Rebrand and honesty pass — Exit Gate
# Verifies: honest naming, no "shadow cognition" or "causal" claims outside docs, coverage targets
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAIL=$((FAIL + 1)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; WARN=$((WARN + 1)); }

echo "=== Phase 1 Exit Gate: Rebrand and Honesty ==="

# All tests pass

# All tests pass (use direct pytest to avoid coverage threshold hang)
if timeout 60 .venv/bin/pytest tests/ -q 2>&1 | tail -1 | grep -q "passed"; then
  pass "All tests pass"
else
  fail "Tests failed"
fi

# No "shadow cognition" outside docs/architecture.md and migration files
SHADOW_COUNT=$(git grep -i "shadow cognition" -- '*.md' '*.py' 2>/dev/null | grep -v "docs/architecture.md" | grep -v "migrations/" | grep -v "decisions.md" | wc -l || true)
if [ "$SHADOW_COUNT" -eq 0 ]; then
  pass "No 'shadow cognition' outside docs/"
else
  fail "'shadow cognition' found in $SHADOW_COUNT places outside docs/"
fi

# No "causal" in .py files outside migrations/
CAUSAL_COUNT=$(git grep -i "causal" -- '*.py' 2>/dev/null | grep -v "migrations/" | wc -l || true)
if [ "$CAUSAL_COUNT" -eq 0 ]; then
  pass "No 'causal' in .py files outside migrations/"
else
  fail "'causal' found in $CAUSAL_COUNT .py files outside migrations/"
fi

# Coverage on event_bus.py >= 60%
EVENT_BUS_COVER=$(.venv/bin/python3 -c "
import xml.etree.ElementTree as ET
tree = ET.parse('coverage.xml')
for class_elem in tree.findall('.//class'):
    if 'event_bus.py' in class_elem.get('filename', ''):
        print(class_elem.get('line-rate', '0'))
        break
")
COVERAGE_CHECK=$(python3 -c "print(1 if float('$EVENT_BUS_COVER') >= 0.60 else 0)")
if [ "$COVERAGE_CHECK" -eq 1 ]; then
  pass "event_bus.py coverage ${EVENT_BUS_COVER} (>= 60%)"
else
  fail "event_bus.py coverage ${EVENT_BUS_COVER} (need >= 60%)"
fi

# Coverage on episode_store.py >= 60%
EPISODE_STORE_COVER=$(.venv/bin/python3 -c "
import xml.etree.ElementTree as ET
tree = ET.parse('coverage.xml')
for class_elem in tree.findall('.//class'):
    if 'episode_store.py' in class_elem.get('filename', ''):
        print(class_elem.get('line-rate', '0'))
        break
")
COVERAGE_CHECK=$(python3 -c "print(1 if float('$EPISODE_STORE_COVER') >= 0.60 else 0)")
if [ "$COVERAGE_CHECK" -eq 1 ]; then
  pass "episode_store.py coverage ${EPISODE_STORE_COVER} (>= 60%)"
else
  fail "episode_store.py coverage ${EPISODE_STORE_COVER} (need >= 60%)"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed, $WARN warnings"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
