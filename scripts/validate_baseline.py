"""Accept a completed baseline test run, never an incomplete measurement."""

import argparse
import json
from pathlib import Path
import sys

from check_coverage import load_report


def validate(status, exit_code):
    if exit_code not in (0, 1):
        raise ValueError(f"baseline runner exited with {exit_code}")
    if not isinstance(status, dict) or status.get("complete") is not True:
        raise ValueError("baseline test run did not complete safely")
    tests, failures = status.get("tests"), status.get("failures")
    if type(tests) is not int or tests <= 0:
        raise ValueError("baseline must collect tests")
    if type(failures) is not int or not 0 <= failures <= tests:
        raise ValueError("invalid baseline failure count")
    if status.get("errors") != []:
        raise ValueError("baseline contains collection, hook or runner errors")
    if (exit_code == 1) != (failures > 0):
        raise ValueError("baseline exit code and test results disagree")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("backend", "frontend"), required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args()
    try:
        failures = validate(json.loads(args.status.read_text()), args.exit_code)
        load_report(args.coverage, args.suite)
    except (OSError, ValueError) as exc:
        print(f"Invalid baseline: {exc}", file=sys.stderr)
        return 1
    if failures:
        print(f"::warning::Baseline completed with {failures} failing tests; coverage comparison remains required.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
