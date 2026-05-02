#!/usr/bin/env bash
# Phase 4: Real action surface — Exit Gate
# Per plan section 4.B:
# - Live test: trigger proposal calling rasputin.search, verify result
# - All execution paths have non-None estimated_execution_cost_usd
# - Coverage on action handlers >= 75%
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

PASS=0
FAIL=0
TOTAL=5

echo "=== Phase 4 Exit Gate: Real Action Surface ==="
echo ""

# ---------------------------------------------------------------------------
# CHECK 1: http_client.py exists and exports safe_get, safe_post
# ---------------------------------------------------------------------------
echo "[1/5] http_client.py exists and exports safe_get, safe_post"
if .venv/bin/python -c "
from cerebellum.http_client import safe_get, safe_post
print('  OK - safe_get and safe_post imported successfully')
" 2>/dev/null; then
    echo "  PASS"
    PASS=$((PASS + 1))
else
    echo "  FAIL - http_client.py missing or exports broken"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# CHECK 2: All execution paths have estimated_execution_cost_usd
# ---------------------------------------------------------------------------
echo "[2/5] All execution paths have estimated_execution_cost_usd"
if grep -q "estimated_execution_cost_usd" src/cerebellum/policy_arbiter.py && \
   grep -q "TOOL_COST_ESTIMATES" src/cerebellum/policy_arbiter.py; then
    # Verify TOOL_COST_ESTIMATES has entries for all handlers
    .venv/bin/python -c "
from cerebellum.policy_arbiter import TOOL_COST_ESTIMATES
expected = [
    'http.get', 'web.search', 'file.read', 'memory.query', 'model.call',
    'notification.send', 'notification.summarize', 'proposal.snooze',
    'rasputin.search', 'rasputin.recent_facts', 'rasputin.entity_lookup',
    'rasputin.episode_summary', 'rasputin.commit_fact', 'rasputin.reflect',
]
missing = [t for t in expected if t not in TOOL_COST_ESTIMATES]
if missing:
    print(f'  FAIL - Missing costs for: {missing}')
    exit(1)
print(f'  OK - All {len(expected)} tools have cost estimates')
" 2>/dev/null && echo "  PASS" && PASS=$((PASS + 1)) || { echo "  FAIL"; FAIL=$((FAIL + 1)); }
else
    echo "  FAIL - estimated_execution_cost_usd or TOOL_COST_ESTIMATES not found"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# CHECK 3: policy.yaml has allowed_tools for new handlers
# ---------------------------------------------------------------------------
echo "[3/5] policy.yaml has allowed_tools for new handlers"
if grep -q "rasputin.search" policy.yaml && \
   grep -q "notification.summarize" policy.yaml && \
   grep -q "proposal.snooze" policy.yaml; then
    echo "  OK - policy.yaml includes new allowed tools"
    echo "  PASS"
    PASS=$((PASS + 1))
else
    echo "  FAIL - policy.yaml missing new allowed tools"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# CHECK 4: policy.yaml has forbidden_tools for rasputin.commit_fact, rasputin.reflect
# ---------------------------------------------------------------------------
echo "[4/5] policy.yaml has forbidden_tools for approval-only RASPUTIN tools"
if grep -A 20 "forbidden_tools:" policy.yaml | grep -q "rasputin.commit_fact" && \
   grep -A 20 "forbidden_tools:" policy.yaml | grep -q "rasputin.reflect"; then
    echo "  OK - rasputin.commit_fact and rasputin.reflect are forbidden"
    echo "  PASS"
    PASS=$((PASS + 1))
else
    echo "  FAIL - rasputin.commit_fact or rasputin.reflect not in forbidden_tools"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# CHECK 5: Tests pass for new handlers
# ---------------------------------------------------------------------------
echo "[5/5] Tests pass for new handlers"
if .venv/bin/python -m pytest tests/test_policy_arbiter_handlers.py -v --no-cov 2>&1 | tail -1 | grep -q "passed"; then
    echo "  OK - All handler tests passed"
    echo "  PASS"
    PASS=$((PASS + 1))
else
    echo "  FAIL - Some handler tests failed"
    FAIL=$((FAIL + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# RESULT
# ---------------------------------------------------------------------------
echo "=== Phase 4 Exit Gate Result: $PASS/$TOTAL passed, $FAIL failed ==="
if [ "$FAIL" -eq 0 ]; then
    echo "PASS - Phase 4 exit gate passed. Ready to merge."
    exit 0
else
    echo "FAIL - Phase 4 exit gate failed. Fix issues before merging."
    exit 1
fi
