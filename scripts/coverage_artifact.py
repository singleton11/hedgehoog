"""Seal or validate coverage artifacts after successful run-completion validation.

A seal binds coverage and validated completion status to their CI identity.
Completed test-body failures are allowed; a seal does not imply all tests passed.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from check_coverage import _unique_object, load_report
from validate_baseline import validate as validate_completion


SCHEMA_VERSION = 1
# Bump when reporter/instrumentation semantics change, invalidating older artifacts.
INSTRUMENTATION_VERSION = 1
METADATA_FILENAME = "metadata.json"
REPORT_FILENAMES = {
    "backend": "coverage.json",
    "frontend": "coverage-summary.json",
}


class ArtifactError(ValueError):
    """An artifact cannot safely be used as a coverage baseline."""


def _report_digest(directory, suite):
    report = directory / REPORT_FILENAMES[suite]
    load_report(report, suite)
    return hashlib.sha256(report.read_bytes()).hexdigest()


def _status_digest(directory):
    path = directory / "test-status.json"
    try:
        status = json.loads(path.read_text(encoding="utf-8"),
                            object_pairs_hook=_unique_object)
        failures = status.get("failures") if isinstance(status, dict) else None
        exit_code = 1 if type(failures) is int and failures > 0 else 0
        validate_completion(status, exit_code)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ArtifactError(f"{path}: invalid completion status: {exc}") from exc
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_metadata(directory, suite, sha, branch, run_id):
    for name, value in (("sha", sha), ("branch", branch), ("run_id", run_id)):
        if not isinstance(value, str) or not value.strip():
            raise ArtifactError(f"{name} must be a nonempty string")
    return {
        "schema_version": SCHEMA_VERSION,
        "instrumentation_version": INSTRUMENTATION_VERSION,
        "suite": suite,
        "sha": sha,
        "branch": branch,
        "run_id": run_id,
        "report_sha256": _report_digest(directory, suite),
        "status_sha256": _status_digest(directory),
    }


def seal(directory, suite, sha, branch, run_id):
    metadata = artifact_metadata(directory, suite, sha, branch, run_id)
    # Publish the manifest atomically, never leaving a partially written seal.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=directory, delete=False) as output:
            temporary = Path(output.name)
            json.dump(metadata, output, indent=2, sort_keys=True)
            output.write("\n")
        temporary.replace(directory / METADATA_FILENAME)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate(directory, suite, sha, branch, run_id):
    path = directory / METADATA_FILENAME
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"),
                              object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ArtifactError(f"{path}: cannot read metadata: {exc}") from exc
    expected = artifact_metadata(directory, suite, sha, branch, run_id)
    if not isinstance(metadata, dict) or metadata.keys() != expected.keys():
        raise ArtifactError(f"{path}: metadata must contain exactly {', '.join(expected)}")
    for key, value in expected.items():
        if type(metadata[key]) is not type(value) or metadata[key] != value:
            raise ArtifactError(f"{path}: {key} mismatch (expected {value!r}, got {metadata[key]!r})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seal", "validate"))
    parser.add_argument("--suite", required=True, choices=REPORT_FILENAMES)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        operation = seal if args.command == "seal" else validate
        operation(args.directory, args.suite, args.sha, args.branch, args.run_id)
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"Coverage artifact {args.command} failed: {exc}", file=sys.stderr)
        return 2
    print(f"Coverage artifact {args.command} succeeded: {args.directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
