#!/usr/bin/env bash
# Supply chain security checks for CEREBELLUM
# Usage: bash scripts/supply_chain.sh
set -euo pipefail

PASS=0
FAIL=0
WARN=0

pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN + 1)); }

echo "=== Supply Chain Security Checks ==="

# 1. pip-audit (pyproject.lock or requirements)
echo ""
echo "[1/4] Checking for known vulnerable dependencies (pip-audit)…"
if command -v pip-audit &>/dev/null; then
    if pip-audit -q 2>&1; then
        pass "pip-audit: no known vulnerabilities"
    else
        fail "pip-audit: vulnerabilities found"
    fi
else
    warn "pip-audit not installed (install with: pip install pip-audit)"
fi

# 2. Bandit (static security analysis)
echo ""
echo "[2/4] Running Bandit security linter…"
if command -v bandit &>/dev/null; then
    if bandit -r src/cerebellum/ -ll -q 2>&1; then
        pass "bandit: no high/medium severity issues"
    else
        fail "bandit: security issues found"
    fi
else
    warn "bandit not installed (install with: pip install bandit)"
fi

# 3. Gitleaks (secret scanning)
echo ""
echo "[3/4] Scanning for leaked secrets (gitleaks)…"
if command -v gitleaks &>/dev/null; then
    if gitleaks detect --source=. --no-git -v 2>&1 | grep -q "No leaks"; then
        pass "gitleaks: no secrets detected"
    else
        fail "gitleaks: potential secrets found"
    fi
else
    warn "gitleaks not installed (see: https://github.com/gitleaks/gitleaks)"
fi

# 4. Dependency pinning check
echo ""
echo "[4/4] Checking dependency pinning in pyproject.toml…"
if grep -q "dependencies" pyproject.toml; then
    # Check for unpinned dependencies (no version specifier)
    UNPINNED=$(grep -A 100 "dependencies" pyproject.toml | grep '"' | grep -v ">=" | grep -v "==" | grep -v "<" | grep -v "#" | head -5 || true)
    if [ -z "$UNPINNED" ]; then
        pass "dependency pinning: all dependencies have version specifiers"
    else
        fail "dependency pinning: unpinned dependencies found: $UNPINNED"
    fi
else
    warn "no dependencies section found in pyproject.toml"
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed, $WARN warnings ==="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
