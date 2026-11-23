#!/usr/bin/env python3
"""List measured attempts with evidence of an abnormal or premature worker exit."""
import argparse
import json
import math
from pathlib import Path
import sys


def premature_exit_reason(result):
    # The observed exit is the parent's clock. wall_s can equal the nominal
    # budget by construction after a watchdog kill; use it only for old records.
    actual = result.get("observed_worker_exit_s")
    if actual is None:
        actual = result.get("wall_s")
    budget = result.get("seconds")
    returned = result.get("returncode") == 0 and (result.get("termination") or {}).get("status") == "returned"
    if actual is not None and budget is not None and not returned:
        tolerance = min(1.0, budget * 0.05)
        if not math.isfinite(actual) or actual < budget - tolerance:
            return f"worker exited after {actual:g}s of {budget:g}s without a normal return"
    return None


def assessment_issues(result):
    if result.get("status") not in {"pass", "no_feasible_within_budget"}:
        return []
    issues = []
    code = result.get("returncode")
    if code not in (None, 0) and result.get("watchdog") is not True:
        issues.append(f"worker exit {code} outside the budget watchdog")

    early = premature_exit_reason(result)
    if early:
        issues.append(early)
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="result directory to scan, including past attempts")
    args = parser.parse_args()
    if not args.out.is_dir():
        parser.error(f"no such result directory: {args.out}")
    failed = False
    for path in sorted(args.out.rglob("result.json")):
        try:
            issues = assessment_issues(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError, AttributeError) as error:
            print(f"{path}: {error}", file=sys.stderr)
            failed = True
            continue
        if issues:
            print(f"{path}\t" + "; ".join(issues))
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
