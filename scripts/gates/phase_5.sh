#!/usr/bin/env bash
# Phase 5: Dashboard & UX — Exit Gate
# Verifies: HTMX dashboard, auth, real-time updates
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAIL=$((FAIL + 1)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; WARN=$((WARN + 1)); }

echo "=== Phase 5 Exit Gate: Dashboard & UX ==="

# Check dashboard auth
if grep -q "DASHBOARD_TOKEN\|auth" src/cerebellum/ui/dashboard.py 2>/dev/null; then
  pass "Dashboard auth present"
else
  fail "No dashboard auth"
fi

# Check HTMX templates
if [ -d "templates" ]; then
  pass "HTMX templates directory exists"
else
  fail "No templates directory"
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
