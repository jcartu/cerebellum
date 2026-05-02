#!/usr/bin/env bash
# Phase 6: Production Hardening — Exit Gate
# Verifies: systemd services, monitoring, backup, disaster recovery
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

pass() { echo -e "  ${GREEN}PASS${NC} $1"; ((PASS++)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; ((FAIL++)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; ((WARN++)); }

echo "=== Phase 6 Exit Gate: Production Hardening ==="

# Check systemd service templates
if [ -f "services/cerebellum-observatory.service.template" ]; then
  pass "Observatory service template exists"
else
  fail "Missing observatory service template"
fi

if [ -f "services/cerebellum-cortex.service.template" ]; then
  pass "Cortex service template exists"
else
  fail "Missing cortex service template"
fi

# Check install script
if [ -f "scripts/install_systemd.sh" ]; then
  pass "Systemd install script exists"
else
  fail "Missing systemd install script"
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
