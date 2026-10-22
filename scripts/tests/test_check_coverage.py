"""Tests for exact coverage comparison and the command-line gate."""

from fractions import Fraction
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "check_coverage.py"
SPEC = importlib.util.spec_from_file_location("check_coverage", SCRIPT)
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)


def backend(total=100, covered=90, branches=10, covered_branches=8):
    return {"totals": {"num_statements": total, "covered_lines": covered,
                       "num_branches": branches, "covered_branches": covered_branches}}


def frontend(total=100, covered=90):
    return {"total": {metric: {"total": total, "covered": covered}
                      for metric in coverage.METRICS["frontend"]}}


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def report(self, data, name="report.json"):
        path = self.directory / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def cli(self, suite, current, baseline=None, summary=None):
        command = [sys.executable, str(SCRIPT), "--suite", suite,
                   "--current", str(current)]
        if baseline is not None:
            command += ["--baseline", str(baseline)]
        env = os.environ.copy()
        env.pop("GITHUB_STEP_SUMMARY", None)
        if summary is not None:
            env["GITHUB_STEP_SUMMARY"] = str(summary)
        return subprocess.run(command, capture_output=True, text=True, env=env)

    def test_equality_and_improvement_for_both_suites(self):
        for suite, factory in (("backend", backend), ("frontend", frontend)):
            baseline = coverage.load_report(self.report(factory()), suite)
            for covered in (90, 91):
                with self.subTest(suite=suite, covered=covered):
                    current = coverage.load_report(self.report(factory(covered=covered)), suite)
                    markdown, regressed = coverage.render_report(suite, current, baseline)
                    self.assertFalse(regressed)
                    self.assertIn("PASS", markdown)

    def test_each_metric_regresses_independently(self):
        for suite in coverage.METRICS:
            for metric in coverage.METRICS[suite]:
                with self.subTest(suite=suite, metric=metric):
                    baseline = dict.fromkeys(coverage.METRICS[suite], Fraction(9, 10))
                    current = dict.fromkeys(coverage.METRICS[suite], Fraction(1))
                    current[metric] = Fraction(89, 100)
                    markdown, regressed = coverage.render_report(suite, current, baseline)
                    self.assertTrue(regressed)
                    self.assertIn("REGRESSION", markdown)

    def test_regression_smaller_than_rounding(self):
        baseline = coverage.load_report(self.report(backend(10**12, 9 * 10**11)), "backend")
        current = coverage.load_report(self.report(backend(10**12, 9 * 10**11 - 1)), "backend")
        markdown, regressed = coverage.render_report("backend", current, baseline)
        self.assertTrue(regressed)
        self.assertIn("-1/10000000000 pp", markdown)

    def test_zero_totals_are_full_coverage(self):
        for suite, factory in (("backend", lambda: backend(0, 0, 0, 0)),
                               ("frontend", lambda: frontend(0, 0))):
            with self.subTest(suite=suite):
                result = coverage.load_report(self.report(factory()), suite)
                self.assertTrue(all(value == 1 for value in result.values()))

    def test_invalid_counts(self):
        for suite, factory in (("backend", backend), ("frontend", frontend)):
            for field in ("total", "covered"):
                for value in (True, False, 1.0, "1", None, -1):
                    with self.subTest(suite=suite, field=field, value=value):
                        data = factory()
                        counts = data["totals"] if suite == "backend" else data["total"]["lines"]
                        key = ("num_statements" if field == "total" else "covered_lines") if suite == "backend" else field
                        counts[key] = value
                        with self.assertRaises(coverage.ReportError):
                            coverage.load_report(self.report(data), suite)
            with self.assertRaises(coverage.ReportError):
                coverage.load_report(self.report(factory(total=1, covered=2)), suite)

    def test_missing_required_counts_and_metrics(self):
        for suite, factory in (("backend", backend), ("frontend", frontend)):
            for metric, keys in coverage.METRICS[suite].items():
                for key in keys:
                    data = factory()
                    counts = data["totals"] if suite == "backend" else data["total"][metric]
                    del counts[key]
                    with self.subTest(suite=suite, metric=metric, key=key):
                        with self.assertRaises(coverage.ReportError):
                            coverage.load_report(self.report(data), suite)
                if suite == "frontend":
                    data = factory()
                    del data["total"][metric]
                    with self.assertRaises(coverage.ReportError):
                        coverage.load_report(self.report(data), suite)

    def test_malformed_reports(self):
        for suite in coverage.METRICS:
            for data in (None, [], {}, {"totals": []}, {"total": []}):
                with self.subTest(suite=suite, data=data):
                    with self.assertRaises(coverage.ReportError):
                        coverage.load_report(self.report(data), suite)
        for text in ("{", '{"totals": {}, "totals": {}}'):
            path = self.directory / "invalid.json"
            path.write_text(text)
            with self.assertRaises(coverage.ReportError):
                coverage.load_report(path, "backend")
        with self.assertRaises(coverage.ReportError):
            coverage.load_report(self.directory / "missing.json", "backend")

    def test_cli_report_only_and_summary_append(self):
        current = self.report(frontend())
        summary = self.directory / "summary.md"
        summary.write_text("Existing summary\n")
        result = self.cli("frontend", current, summary=summary)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REPORT ONLY", result.stdout)
        self.assertIn("| Baseline | Current | Delta | Status |", result.stdout)
        self.assertEqual(summary.read_text(), "Existing summary\n" + result.stdout + "\n")

    def test_cli_comparison_exit_codes(self):
        for suite, factory in (("backend", backend), ("frontend", frontend)):
            baseline = self.report(factory(), "baseline.json")
            for covered, expected in ((90, 0), (91, 0), (89, 1)):
                with self.subTest(suite=suite, covered=covered):
                    result = self.cli(suite, self.report(factory(covered=covered)), baseline)
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_cli_invalid_current_and_baseline_fail_closed(self):
        valid = self.report(backend())
        missing = self.directory / "missing.json"
        for current, baseline in ((missing, None), (valid, missing)):
            result = self.cli("backend", current, baseline)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Coverage check failed:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
