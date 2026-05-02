#!/usr/bin/env bash
# Install systemd user units for cerebellum, with template substitution.
# Idempotent: re-run safe.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
SERVICES_DIR="$REPO_ROOT/services"
USER_UNIT_DIR="$HOME/.config/systemd/user"

CEREBELLUM_BASE_DIR="${CEREBELLUM_BASE_DIR:-$HOME/.cerebellum}"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: Python binary not found or not executable: $PYTHON_BIN"
  echo "Hint: run 'make install' first, or set PYTHON_BIN=/path/to/python"
  exit 1
fi

mkdir -p "$USER_UNIT_DIR"
mkdir -p "$CEREBELLUM_BASE_DIR"

substitute() {
  local template="$1"
  local target="$2"
  sed \
    -e "s|__CEREBELLUM_BASE_DIR__|$CEREBELLUM_BASE_DIR|g" \
    -e "s|__USER__|$USER|g" \
    -e "s|__PYTHON__|$PYTHON_BIN|g" \
    "$template" > "$target"
}

for tmpl in "$SERVICES_DIR"/*.service.template; do
  base=$(basename "$tmpl" .template)
  out="$USER_UNIT_DIR/$base"
  substitute "$tmpl" "$out"
  echo "Installed: $out"
done

systemctl --user daemon-reload

echo
echo "Next steps:"
echo "  1. Copy your .env to $CEREBELLUM_BASE_DIR/.env (chmod 0600)"
echo "  2. Copy config.example.json to $CEREBELLUM_BASE_DIR/config.json and edit"
echo "  3. systemctl --user enable --now cerebellum-observatory cerebellum-cortex"
echo "  4. journalctl --user -u cerebellum-observatory -f"
