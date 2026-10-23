"""Compare coverage reports without allowing rounded percentages to hide regressions."""

import argparse
from fractions import Fraction
import json
import os
from pathlib import Path
import sys


METRICS = {
    "backend": {
        "lines": ("num_statements", "covered_lines"),
        "branches": ("num_branches", "covered_branches"),
    },
    "frontend": {
        name: ("total", "covered")
        for name in ("statements", "lines", "functions", "branches")
    },
}


class ReportError(ValueError):
    """A report cannot safely be used for coverage comparison."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReportError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_report(path, suite):
    """Read required metrics, validating raw counts before constructing ratios."""
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"),
                            object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ReportError(f"{path}: cannot read coverage report: {exc}") from exc
    root_key = "totals" if suite == "backend" else "total"
    if not isinstance(report, dict) or not isinstance(report.get(root_key), dict):
        raise ReportError(f"{path}: missing or invalid {root_key} object")
    totals = report[root_key]
    result = {}
    for metric, (total_key, covered_key) in METRICS[suite].items():
        counts = totals if suite == "backend" else totals.get(metric)
        if not isinstance(counts, dict):
            raise ReportError(f"{path}: missing or invalid {metric} metric")
        total, covered = counts.get(total_key), counts.get(covered_key)
        if (type(total) is not int or type(covered) is not int
                or not 0 <= covered <= total):
            raise ReportError(
                f"{path}: {metric} requires integer counts 0 <= {covered_key} <= {total_key}"
            )
        result[metric] = Fraction(covered, total) if total else Fraction(1)
    return result


def _percentage(ratio):
    return f"{float(ratio * 100):.6f}%"


def _delta(difference):
    # Exact fractional percentage points also expose changes below display precision.
    value = difference * 100
    return f"{'+' if value > 0 else ''}{value} pp"


def render_report(suite, current, baseline=None):
    """Return Markdown and whether any individual metric regressed."""
    rows = [f"## {suite.capitalize()} coverage", "",
            "| Metric | Baseline | Current | Delta | Status |",
            "| --- | ---: | ---: | ---: | --- |"]
    regressed = False
    for metric, ratio in current.items():
        if baseline is None:
            previous, delta, status = "N/A", "N/A", "REPORT ONLY"
        else:
            difference = ratio - baseline[metric]
            previous, delta = _percentage(baseline[metric]), _delta(difference)
            status = "REGRESSION" if difference < 0 else "PASS"
            regressed = regressed or difference < 0
        rows.append(f"| {metric} | {previous} | {_percentage(ratio)} | {delta} | {status} |")
    return "\n".join(rows) + "\n", regressed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, choices=METRICS)
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args(argv)
    try:
        current = load_report(args.current, args.suite)
        baseline = load_report(args.baseline, args.suite) if args.baseline is not None else None
        markdown, regressed = render_report(args.suite, current, baseline)
        print(markdown, end="")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a", encoding="utf-8") as output:
                output.write(markdown + "\n")
    except (ReportError, OSError, UnicodeError) as exc:
        print(f"Coverage check failed: {exc}", file=sys.stderr)
        return 2
    return 1 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
