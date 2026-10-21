"""Completed baseline failures cannot hide incomplete runs or bad coverage."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from validate_baseline import validate


class ValidateBaselineTests(unittest.TestCase):
    def test_complete_runs(self):
        for failures, code in ((0, 0), (2, 1)):
            self.assertEqual(validate({"complete": True, "tests": 3,
                                       "failures": failures, "errors": []}, code), failures)

    def test_reject_invalid_runs(self):
        valid = {"complete": True, "tests": 3, "failures": 1, "errors": []}
        for changes, code in (({}, 2), ({}, 137), ({}, 0),
                              ({"complete": False}, 1), ({"tests": 0}, 1),
                              ({"tests": True}, 1), ({"failures": True}, 1),
                              ({"failures": 4}, 1), ({"errors": ["hook"]}, 1),
                              ({"failures": 0}, 1)):
            with self.subTest(changes=changes, code=code), self.assertRaises(ValueError):
                validate({**valid, **changes}, code)

    def test_cli_requires_fresh_valid_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            status, coverage = directory / "status.json", directory / "coverage.json"
            status.write_text(json.dumps({"complete": True, "tests": 2,
                                          "failures": 1, "errors": []}))
            command = [sys.executable, str(SCRIPTS / "validate_baseline.py"),
                       "--suite", "backend", "--status", str(status),
                       "--coverage", str(coverage), "--exit-code", "1"]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            coverage.write_text(json.dumps({"totals": {"num_statements": 10,
                "covered_lines": 5, "num_branches": 2, "covered_branches": 1}}))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("warning", result.stdout)
            coverage.write_text("{}")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
