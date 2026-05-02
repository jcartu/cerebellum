#!/usr/bin/env bash
# Phase 0 exit gate: clean checkout must install, lint, and test.
# Mypy strict is deferred to Phase 6 per plan.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

echo "==> Phase 0 exit gate"
echo "    Repo: $REPO_ROOT"
echo "    Python: $(python3 --version)"

# 1. Required files exist
required=(
  pyproject.toml
  Makefile
  README.md
  decisions.md
  requirements.txt
  src/cerebellum/__init__.py
  scripts/install_systemd.sh
  .github/workflows/ci.yml
  services/cerebellum-observatory.service.template
  services/cerebellum-cortex.service.template
)

for f in "${required[@]}"; do
  if [[ ! -e "$f" ]]; then
    echo "FAIL: missing required file: $f"
    exit 1
  fi
done
echo "OK: all required files present"

# 2. Forbidden files are gone
forbidden=(
  cerebellum-plan.md
  __init__.py             # root-level
  cerebellum/observatory_main.py  # the duplicate package shim
)

for f in "${forbidden[@]}"; do
  if [[ -e "$f" ]]; then
    echo "FAIL: forbidden file still present: $f"
    exit 1
  fi
done
echo "OK: forbidden files removed"

# 3. No hardcoded /home/josh paths in source
hardcoded=$(grep -rn "/home/josh" src tests scripts services Makefile 2>/dev/null | grep -v Binary | grep -v "phase_0.sh" || true)
if [[ -n "$hardcoded" ]]; then
  echo "FAIL: hardcoded /home/josh paths found:"
  echo "$hardcoded"
  exit 1
fi
echo "OK: no /home/josh hardcoding"

# 4. Model identifier sanity (Phase 0 task 9)
bad_models=$(grep -rn "claude-opus-4\.7\|claude-3\.5-sonnet" src tests README.md 2>/dev/null || true)
if [[ -n "$bad_models" ]]; then
  echo "FAIL: invalid model identifiers found (use claude-opus-4-7, claude-sonnet-4-6):"
  echo "$bad_models"
  exit 1
fi
echo "OK: model identifiers normalized"

# 5. Quality gates (mypy deferred to Phase 6 per plan)
make lint
echo "OK: lint"

# make typecheck  # Deferred — mypy strict required by Phase 6 only
echo "SKIP: typecheck (deferred to Phase 6)"

make test
echo "OK: tests"

# 6. Coverage report exists
if [[ ! -f coverage.xml ]]; then
  echo "FAIL: coverage.xml not produced by test run"
  exit 1
fi
echo "OK: coverage.xml present"

# 7. Fresh-install smoke test in a tempdir (proves the README quick-start works)
TMPDIR_GATE=$(mktemp -d)
trap 'rm -rf "$TMPDIR_GATE"' EXIT

git clone --quiet "$REPO_ROOT" "$TMPDIR_GATE/cerebellum"
cd "$TMPDIR_GATE/cerebellum"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[dev]"
.venv/bin/python -c "import cerebellum; print(cerebellum.__name__)"
echo "OK: clean-checkout install + import"

cd "$REPO_ROOT"
echo
echo "==> Phase 0 exit gate PASS"
