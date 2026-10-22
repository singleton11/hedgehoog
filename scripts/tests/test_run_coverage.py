"""Exercise runner exit handling without running application test suites."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class CoverageRunnerTests(unittest.TestCase):
    def run_runner(self, suite, mode, code=1, complete=True, report=True, status=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("backend", "frontend", "bin", "reports"):
                (root / name).mkdir()
            # Simulate a runner producing fresh metadata and coverage, then exiting.
            program = """#!/usr/bin/env python3
import json, os
from pathlib import Path
path = os.environ.get('BASELINE_STATUS_FILE')
if path and os.environ['STATUS'] == '1':
    Path(path).write_text(json.dumps({'complete': os.environ['COMPLETE'] == '1',
        'tests': 2, 'failures': 1, 'errors': []}))
if os.environ['REPORT'] == '1':
    Path(os.environ['COVERAGE_PATH']).write_text(os.environ['COVERAGE_JSON'])
raise SystemExit(int(os.environ['RUNNER_CODE']))
"""
            for name in ("uv", "npm"):
                executable = root / "bin" / name
                executable.write_text(program)
                executable.chmod(0o755)
            filename = "coverage.json" if suite == "backend" else "coverage-summary.json"
            coverage = root / "reports" / filename
            # A stale report must not rescue a baseline runner that writes no report.
            totals = {"num_statements": 2, "covered_lines": 1,
                      "num_branches": 2, "covered_branches": 1}
            data = {"totals": totals} if suite == "backend" else {
                "total": {metric: {"total": 2, "covered": 1}
                          for metric in ("statements", "lines", "functions", "branches")}}
            coverage.write_text(json.dumps(data))
            (root / "reports/test-status.json").write_text(json.dumps({
                "complete": True, "tests": 2, "failures": 1, "errors": []}))
            env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}",
                       COMPLETE=str(int(complete)), REPORT=str(int(report)), STATUS=str(int(status)),
                       RUNNER_CODE=str(code), COVERAGE_PATH=str(coverage),
                       COVERAGE_JSON=json.dumps(data))
            return subprocess.run(["bash", str(ROOT / "scripts/run_coverage.sh"),
                                   suite, str(root), str(root / "reports"), mode],
                                  env=env, capture_output=True, text=True, timeout=15)

    def test_current_failures_remain_failures(self):
        for suite in ("backend", "frontend"):
            with self.subTest(suite=suite):
                self.assertEqual(self.run_runner(suite, "current").returncode, 1)

    def test_completed_baseline_failures_are_measurements(self):
        for suite in ("backend", "frontend"):
            with self.subTest(suite=suite):
                result = self.run_runner(suite, "baseline")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("warning", result.stdout)

    def test_baseline_rejects_incomplete_crashed_and_stale_reports(self):
        for suite in ("backend", "frontend"):
            for options in ({"complete": False}, {"code": 137},
                            {"report": False}, {"status": False}):
                with self.subTest(suite=suite, options=options):
                    self.assertNotEqual(self.run_runner(suite, "baseline", **options).returncode, 0)


if __name__ == "__main__":
    unittest.main()
