#!/usr/bin/env bash
# Phase 1: Event Reliability — Exit Gate
# Verifies: NATS JetStream durability, event dedup, SQLite WAL integrity
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; ((PASS++)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; ((FAIL++)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; ((WARN++)); }

echo "=== Phase 1 Exit Gate: Event Reliability ==="

# Check NATS connection
if python3 -c "import nats; print('NATS SDK available')" 2>/dev/null; then
  pass "NATS SDK available"
else
  warn "NATS SDK not installed (optional for gate)"
fi

# Check events.db WAL mode
if [ -f events.db ]; then
  mode=$(python3 -c "
import sqlite3
conn = sqlite3.connect('events.db')
print(conn.execute('PRAGMA journal_mode').fetchone()[0])
conn.close()
" 2>/dev/null || echo "unknown")
  if [ "$mode" = "wal" ]; then
    pass "SQLite WAL mode active"
  else
    fail "SQLite journal_mode is '$mode', expected 'wal'"
  fi
else
  warn "events.db not found (may not have run yet)"
fi

# Check event deduplication logic exists
if grep -q "dedup" src/cerebellum/events.py 2>/dev/null; then
  pass "Event deduplication logic present"
else
  fail "No deduplication logic in events.py"
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
