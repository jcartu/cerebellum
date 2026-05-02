#!/usr/bin/env bash
# Phase 2: Hypothesis grounding — Exit Gate (TODO: implement)
# Per plan section 2.B:
# - New behavioral tests pass
# - Run a 2-hour live integration with synthetic events: 99%+ of accepted proposals
#   have non-empty evidence_event_ids that all exist in the event store
# - Verifier disagreement rate logged to decisions.md as a baseline
# - Coverage on proposer.py + grounding.py >= 70%
set -euo pipefail

echo "=== Phase 2 Exit Gate: Hypothesis Grounding ==="
echo ""
echo "TODO: implement phase 2 gate checks"
echo "  - [ ] Behavioral tests pass"
echo "  - [ ] 2h synthetic integration: 99%+ proposals have valid evidence_event_ids"
echo "  - [ ] Verifier disagreement rate logged"
echo "  - [ ] proposer.py + grounding.py coverage >= 70%"
echo ""
echo "Exiting with failure until implemented."
exit 1
