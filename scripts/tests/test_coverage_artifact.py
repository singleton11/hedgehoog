"""Stdlib-only integration tests for coverage artifact sealing and validation."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "coverage_artifact.py"


class CoverageArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.status = {"complete": True, "tests": 10, "failures": 0, "errors": []}
        self.write("test-status.json", self.status)
        self.write("coverage.json", {"totals": {
            "num_statements": 10, "covered_lines": 9,
            "num_branches": 4, "covered_branches": 3}})
        self.write("coverage-summary.json", {"total": {
            metric: {"total": 10, "covered": 9}
            for metric in ("statements", "lines", "functions", "branches")}})

    def write(self, name, value):
        (self.directory / name).write_text(json.dumps(value), encoding="utf-8")

    def cli(self, command, suite="backend", **overrides):
        options = {"suite": suite, "sha": "abc123", "directory": str(self.directory),
                   "branch": "main", "run-id": "123"}
        options.update(overrides)
        args = [sys.executable, str(SCRIPT), command]
        for key, value in options.items():
            if value is not None:
                args.extend(["--" + key, value])
        return subprocess.run(args, capture_output=True, text=True)

    def succeeds(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("succeeded", result.stdout)

    def fails(self, result, text=None):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if text:
            self.assertIn(text, result.stderr)

    def test_round_trip_and_manifest_contract(self):
        for suite, filename in (("backend", "coverage.json"),
                                ("frontend", "coverage-summary.json")):
            with self.subTest(suite=suite):
                self.succeeds(self.cli("seal", suite))
                metadata = json.loads((self.directory / "metadata.json").read_text())
                self.assertEqual(metadata, {
                    "schema_version": 1, "instrumentation_version": 1,
                    "suite": suite, "sha": "abc123", "branch": "main", "run_id": "123",
                    "report_sha256": hashlib.sha256((self.directory / filename).read_bytes()).hexdigest(),
                    "status_sha256": hashlib.sha256((self.directory / "test-status.json").read_bytes()).hexdigest(),
                })
                self.succeeds(self.cli("validate", suite))

    def test_completed_test_failures_are_accepted(self):
        self.status["failures"] = 3
        self.write("test-status.json", self.status)
        self.succeeds(self.cli("seal"))
        self.succeeds(self.cli("validate"))

    def test_every_manifest_field_is_required_and_exact(self):
        self.succeeds(self.cli("seal"))
        original = json.loads((self.directory / "metadata.json").read_text())
        for key, value in original.items():
            for replacement in (None, False, 99, "wrong"):
                with self.subTest(key=key, replacement=replacement):
                    self.write("metadata.json", dict(original, **{key: replacement}))
                    self.fails(self.cli("validate"), key)
            missing = original.copy()
            del missing[key]
            self.write("metadata.json", missing)
            self.fails(self.cli("validate"), "exactly")
        self.write("metadata.json", dict(original, extra=True))
        self.fails(self.cli("validate"), "exactly")

    def test_expected_identity_must_match(self):
        self.succeeds(self.cli("seal"))
        for key in ("sha", "branch", "run-id", "suite"):
            self.fails(self.cli("validate", **{key: "frontend" if key == "suite" else "different"}),
                       key.replace("-", "_"))

    def test_byte_changes_in_valid_report_or_status_fail_checksum(self):
        self.succeeds(self.cli("seal"))
        for filename, field in (("coverage.json", "report_sha256"),
                                ("test-status.json", "status_sha256")):
            path = self.directory / filename
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            self.fails(self.cli("validate"), field)
            path.write_bytes(original)

    def test_malformed_or_missing_metadata(self):
        self.fails(self.cli("validate"), "metadata")
        for content in ("{", "[]", "null", '{"schema_version":1,"schema_version":1}', "\xff"):
            (self.directory / "metadata.json").write_bytes(content.encode("latin1"))
            self.fails(self.cli("validate"), "metadata")

    def test_invalid_reports_cannot_be_sealed_or_validated(self):
        for suite, filename in (("backend", "coverage.json"),
                                ("frontend", "coverage-summary.json")):
            path = self.directory / filename
            original = path.read_bytes()
            for content in (None, b"{", b"null", b"{}", b"\xff",
                            b'{"totals":{},"totals":{}}',
                            b'{"totals":{"num_statements":true,"covered_lines":0}}'):
                with self.subTest(suite=suite, content=content):
                    path.write_bytes(original)
                    self.succeeds(self.cli("seal", suite))
                    if content is None:
                        path.unlink()
                    else:
                        path.write_bytes(content)
                    self.fails(self.cli("seal", suite))
                    self.fails(self.cli("validate", suite))
            path.write_bytes(original)

    def test_invalid_completion_cannot_be_sealed_or_validated(self):
        invalid = [None, [], {}, dict(self.status, complete=False),
                   dict(self.status, complete=1), dict(self.status, tests=0),
                   dict(self.status, tests=True), dict(self.status, failures=True),
                   dict(self.status, failures=-1), dict(self.status, failures=11),
                   dict(self.status, failures="1"), dict(self.status, errors=["cleanup failed"]),
                   dict(self.status, errors=None)]
        for status in invalid:
            with self.subTest(status=status):
                self.write("test-status.json", self.status)
                self.succeeds(self.cli("seal"))
                self.write("test-status.json", status)
                self.fails(self.cli("seal"), "completion")
                self.fails(self.cli("validate"), "completion")

    def test_missing_malformed_duplicate_and_non_utf8_status(self):
        for content in (None, b"{", b"\xff", b'{"complete":true,"complete":true}'):
            self.write("test-status.json", self.status)
            self.succeeds(self.cli("seal"))
            path = self.directory / "test-status.json"
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
            self.fails(self.cli("seal"), "completion")
            self.fails(self.cli("validate"), "completion")

    def test_invalid_seal_does_not_publish_metadata(self):
        (self.directory / "test-status.json").unlink()
        self.fails(self.cli("seal"))
        self.assertFalse((self.directory / "metadata.json").exists())

    def test_all_flags_are_required_for_both_commands(self):
        for command in ("seal", "validate"):
            for key in ("suite", "sha", "directory", "branch", "run-id"):
                with self.subTest(command=command, key=key):
                    self.fails(self.cli(command, **{key: None}), "required")

    def test_blank_identity_is_rejected(self):
        for command in ("seal", "validate"):
            self.succeeds(self.cli("seal"))
            for key in ("sha", "branch", "run-id"):
                self.fails(self.cli(command, **{key: " "}), "nonempty")


if __name__ == "__main__":
    unittest.main()
