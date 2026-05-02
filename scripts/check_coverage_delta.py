#!/usr/bin/env python3
"""Check coverage delta between two pytest coverage reports.

Usage:
    python scripts/check_coverage_delta.py baseline.json current.json [--min-delta 0]

Exits 0 if coverage delta >= --min-delta (default 0), non-zero otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_report(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: Coverage report not found: {p}", file=sys.stderr)
        sys.exit(2)
    with open(p) as f:
        return json.load(f)


def compute_coverage(report: dict) -> float:
    """Compute overall line coverage percentage from pytest-cov JSON report."""
    totals = report.get("totals", {})
    if isinstance(totals, dict):
        covered = totals.get("covered_lines", 0)
        missed = totals.get("missing_lines", 0)
        total = covered + missed
        if total == 0:
            return 0.0
        return (covered / total) * 100
    # Fallback: average per-file coverage
    files = report.get("files", {})
    if not files:
        return 0.0
    coverage_sum = 0.0
    for _fname, fdata in files.items():
        covered = fdata.get("covered", 0)
        missed = len(fdata.get("missing", []))
        total = covered + missed
        if total > 0:
            coverage_sum += (covered / total) * 100
    return coverage_sum / len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check coverage delta between two reports")
    parser.add_argument("baseline", help="Baseline coverage report (JSON)")
    parser.add_argument("current", help="Current coverage report (JSON)")
    parser.add_argument(
        "--min-delta",
        type=float,
        default=0.0,
        help="Minimum acceptable coverage delta (default: 0.0)",
    )
    args = parser.parse_args()

    baseline = load_report(args.baseline)
    current = load_report(args.current)

    baseline_cov = compute_coverage(baseline)
    current_cov = compute_coverage(current)
    delta = current_cov - baseline_cov

    print(f"Baseline coverage: {baseline_cov:.1f}%")
    print(f"Current coverage:  {current_cov:.1f}%")
    print(f"Delta:             {delta:+.1f}%")
    print(f"Minimum delta:     {args.min_delta:+.1f}%")

    if delta >= args.min_delta:
        print(f"\n✅ Coverage delta {delta:+.1f}% >= {args.min_delta:+.1f}% — PASS")
        return 0
    else:
        print(f"\n❌ Coverage delta {delta:+.1f}% < {args.min_delta:+.1f}% — FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
