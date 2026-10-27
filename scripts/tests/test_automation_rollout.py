"""Offline Temporal inventory checks; no installed adapters or credentials."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "scripts"))
import check_automation_rollout as check


def load(path):
    return json.loads((ROOT / path).read_bytes())


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load(check.FIXTURE)
        self.batch = load(check.PLAN)
        self.manifest = load(check.MANIFEST)

    def test_repository_contracts_and_pins(self):
        report = check.make_report(repository_only=True)
        self.assertEqual(report["status"], "repository_ok")
        self.assertEqual({r["status"] for r in report["repository"]}, {"match"})
        self.assertEqual({r["status"] for r in report["fixture_checks"]}, {"pass"})
        self.assertEqual({r["status"] for r in report["snapshots"]}, {"not_inspected"})
        self.assertEqual(report["runtime"], "temporal")
        for name, path in check.COMPONENTS.items():
            self.assertEqual(report["mapping"][name]["path"], path)
        self.assertEqual(report["external_behavior"], "not_verified")
        self.assertEqual(report["deployment"], "not_performed")

    def test_default_is_blocked_without_external_evidence(self):
        self.assertEqual(check.make_report()["status"], "blocked")

    def test_manifest_rejects_unknown_paths_and_components(self):
        mutations = [
            lambda m: m.update(runtime="hub"),
            lambda m: m.update(schema_version=True),
            lambda m: m["source_sha256"].update({".env": "0" * 64}),
            lambda m: m["source_sha256"].update({check.PLAN: "not-a-hash"}),
            lambda m: m["components"].update(extra={}),
            lambda m: m["components"]["docs"].update(path="../secret"),
            lambda m: m["components"]["docs"].update(references=[".env"]),
        ]
        for mutate in mutations:
            value = copy.deepcopy(self.manifest)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(check.ContractError):
                check.validate_manifest(value)

    def test_missing_and_stale_dependencies_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in check.SOURCES | {check.MANIFEST}:
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / path).read_bytes())
            helper = root / ".opencode/skills/planning/scripts/build_plan.py"
            helper.write_bytes(b"# synthetic stale helper\n")
            (root / check.PLAN).unlink()
            before = {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in root.rglob("*") if p.is_file()}
            report = check.make_report(root, repository_only=True)
            after = {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
                     for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(report["status"], "blocked")
            rows = {r["path"]: r["status"] for r in report["repository"]}
            self.assertEqual(rows[str(helper.relative_to(root))], "stale")
            self.assertEqual(rows[check.PLAN], "missing")
            self.assertIn({"contract": "native_docs_fixture", "status": "fail"}, report["fixture_checks"])

    def test_snapshot_matching_missing_stale_and_unsafe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = b"# safe synthetic source\nraise RuntimeError('must not execute')\n"
            hashes = {path: check.digest(data) for path in check.COMPONENTS.values()}
            (root / "successor").mkdir()
            for path in check.COMPONENTS.values():
                (root / path).write_bytes(data)
            reader = lambda p: check.read_regular(root, p)
            report = check.make_report(hashes=hashes, reader=reader)
            self.assertEqual(report["status"], "offline_ok")
            self.assertEqual(report["external_behavior"], "not_verified")
            (root / check.COMPONENTS["attention"]).write_bytes(b"# drift\n")
            (root / check.COMPONENTS["service"]).unlink()
            (root / check.COMPONENTS["github"]).unlink()
            (root / check.COMPONENTS["github"]).symlink_to(root / check.COMPONENTS["docs"])
            rows = {r["component"]: r["status"] for r in check.check_snapshots(hashes, reader)}
            self.assertEqual(rows, {"docs": "snapshot_hash_match", "attention": "stale",
                                    "github": "unreadable_or_unsafe", "service": "missing"})
            self.assertEqual(check.make_report(hashes=hashes, reader=reader)["status"], "blocked")

    def test_snapshot_lock_schema_and_allowlist(self):
        value = {"schema_version": 1, "runtime": "temporal",
                 "source_sha256": {p: "a" * 64 for p in check.COMPONENTS.values()}}
        self.assertEqual(check.validate_snapshot_lock(value), value["source_sha256"])
        for mutate in (lambda v: v.update(runtime="legacy"),
                       lambda v: v.update(schema_version=True),
                       lambda v: v.update(token="DO_NOT_PRINT"),
                       lambda v: v["source_sha256"].update({".env": "0" * 64}),
                       lambda v: v["source_sha256"].pop("successor/docs.py"),
                       lambda v: v["source_sha256"].update({"successor/docs.py": "DO_NOT_PRINT"})):
            bad = copy.deepcopy(value)
            mutate(bad)
            with self.assertRaises(check.ContractError):
                check.validate_snapshot_lock(bad)

    def test_bounded_file_reads_no_symlinks_traversal_fifo_or_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "successor").mkdir()
            file = root / "successor/docs.py"
            file.write_bytes(b"safe")
            self.assertEqual(check.read_regular(root, "successor/docs.py"), b"safe")
            for path in ("../secret", "/etc/passwd", "successor/../secret", "./secret", ""):
                with self.assertRaises(check.ContractError):
                    check.read_regular(root, path)
            (root / "link").symlink_to(root / "successor", target_is_directory=True)
            with self.assertRaises(OSError):
                check.read_regular(root, "link/docs.py")
            file.unlink()
            file.symlink_to(root / "missing")
            with self.assertRaises(OSError):
                check.read_regular(root, "successor/docs.py")
            file.unlink()
            os.mkfifo(file)
            with self.assertRaises(check.ContractError):
                check.read_regular(root, "successor/docs.py")
            file.unlink()
            file.mkdir()
            with self.assertRaises((check.ContractError, OSError)):
                check.read_regular(root, "successor/docs.py")
            file.rmdir()
            file.write_bytes(b"x" * (check.LIMIT + 1))
            with self.assertRaises(check.ContractError):
                check.read_regular(root, "successor/docs.py")

    def test_duplicate_json_keys_and_invalid_constants(self):
        for data in (b'{"runtime":"temporal","runtime":"hub"}', b'{"x":NaN}', b'not-json', b'\xff'):
            with self.assertRaises(check.ContractError):
                check.json_bytes(data)

    def test_docs_fixture_rejects_plain_or_mutated_creation_contract(self):
        mutations = [
            lambda b: b["requests"][1]["updateParagraphStyle"]["paragraphStyle"].update(lineSpacing=100),
            lambda b: b["requests"][2]["updateTextStyle"].update(fields="*"),
            lambda b: b["requests"][2]["updateTextStyle"]["range"].update(startIndex=9),
            lambda b: b["requests"][-1]["insertDropdown"].update(selectedOptionId="dropdownItem.ready"),
            lambda b: b["requests"][-2]["createDropdownDefinition"]["dropdownDefinition"]
                       ["dropdownDefinitionProperties"]["options"].pop(),
            lambda b: b["requests"].__setitem__(slice(3, -2), []),
        ]
        for mutate in mutations:
            value = copy.deepcopy(self.batch)
            mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(check.ContractError):
                check.check_docs(value)

    def test_attention_rejects_forged_fixture_fields(self):
        mutations = [
            lambda f: f["callback"].update(schema_version=True),
            lambda f: f["callback"].update(reply={"text": "fabricated"}),
            lambda f: f["callback"]["answer"].update(text="different description"),
            lambda f: f["callback"]["answer"].update(option_id="__other__"),
            lambda f: f["callback"]["context"].update(issue_number=99),
            lambda f: f["callback"]["update"].update(update_id=5),
            lambda f: f["callback"]["callback_query"].update(data="a1:unknown"),
            lambda f: f["callback"]["callback_query"]["from"].update(id=9),
            lambda f: f["callback"]["callback_query"]["message"]["chat"].update(id=9),
            lambda f: f["callback"]["callback_query"]["message"]["from"].update(id=9),
            lambda f: f["callback"]["callback_query"]["message"].update(message_id=9),
            lambda f: f["text"].update(schema_version=1),
            lambda f: f["rendered"].update(parse_mode="Markdown"),
            lambda f: f["rendered"]["reply_markup"]["inline_keyboard"].reverse(),
        ]
        for mutate in mutations:
            value = copy.deepcopy(self.fixture)
            mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(check.ContractError):
                check.check_attention(value)

    def test_fixture_generation_parity_without_network(self):
        path = ROOT / ".opencode/skills/paseo-telegram-attention/scripts"
        sys.path.insert(0, str(path))
        self.addCleanup(lambda: sys.path.remove(str(path)))
        import attention_prompt
        import attention_receiver
        plan = load_module("offline_plan_contract", ".opencode/skills/planning/scripts/build_plan.py")
        with patch.object(socket, "socket", side_effect=AssertionError("no network")):
            self.assertEqual(plan.build_plan(load(".opencode/skills/planning/references/plan-content.json")), self.batch)
            self.assertEqual(attention_prompt.render_prompt(self.fixture["prompt"], self.fixture["tokens"]),
                             self.fixture["rendered"])
            for kind in ("callback", "text"):
                event = self.fixture[kind]
                store = attention_receiver.Store(":memory:", "123")
                try:
                    store.reserve(event["question_id"], {"chat_id": 42, "user_id": 42},
                                  event["context"], self.fixture["prompt"], self.fixture["tokens"])
                    store.bind(event["question_id"], 10)
                    raw = event["update"] if kind == "callback" else {
                        "update_id": event["update_id"], "message": event["reply"]}
                    store.ingest([raw, raw])
                    self.assertEqual(store.ready(0), [event])
                finally:
                    store.close()

    def test_checker_never_imports_sources_reads_credentials_or_calls_network(self):
        original = check.read_regular
        allowed = check.SOURCES | {check.MANIFEST}
        def bounded(root, path):
            self.assertEqual(root, ROOT)
            self.assertIn(path, allowed)
            return original(root, path)
        with patch.object(check, "read_regular", side_effect=bounded), \
             patch.object(socket, "socket", side_effect=AssertionError("no network")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")), \
             patch.object(importlib.util, "spec_from_file_location", side_effect=AssertionError("no source execution")), \
             patch.dict(os.environ, {"SYNTHETIC_SECRET": "DO_NOT_PRINT"}):
            report = check.make_report(repository_only=True)
        self.assertNotIn("DO_NOT_PRINT", json.dumps(report))
        self.assertEqual(report["status"], "repository_ok")

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(ROOT / "scripts/check_automation_rollout.py"), *args],
                              capture_output=True, text=True, cwd=ROOT, timeout=10)

    def test_cli_modes_capture_and_no_checkout_changes(self):
        def inventory():
            paths = check.SOURCES | {check.MANIFEST, "scripts/check_automation_rollout.py",
                                     "scripts/tests/test_automation_rollout.py", "docs/automation-rollout.md"}
            return {p: check.digest(check.read_regular(ROOT, p)) for p in paths}
        before = inventory()
        for args, code, status in (([], 1, "blocked"), (["--repository-only"], 0, "repository_ok"),
                                   (["--demo"], 1, "blocked")):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertEqual(result.stderr, "")
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], status)
            self.assertEqual(report["deployment"], "not_performed")
            if args == ["--demo"]:
                rows = {r["component"]: r["status"] for r in report["snapshots"]}
                self.assertEqual(rows, {"docs": "snapshot_hash_match", "github": "snapshot_hash_match",
                                       "attention": "stale", "service": "missing"})
                self.assertEqual(report["mode"], "synthetic_demo")
        self.assertEqual(before, inventory())

    def test_cli_invalid_input_does_not_echo_contents_or_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "PRIVATE_PATH.json"
            for value in (b'DO_NOT_PRINT', b'{"token":"DO_NOT_PRINT"}', b'[]'):
                lock.write_bytes(value)
                result = self.run_cli("--snapshot-root", directory, "--snapshot-lock", str(lock))
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("DO_NOT_PRINT", result.stdout + result.stderr)
                self.assertNotIn("PRIVATE_PATH", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
            result = self.run_cli("--snapshot-root", directory)
            self.assertEqual(result.returncode, 2)
            self.assertIn("paired_snapshot_arguments", result.stdout)
        for args in (("--snapshot-rooot", "/PRIVATE_PATH/DO_NOT_PRINT"), ("--snapshot-root",)):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("PRIVATE_PATH", result.stdout + result.stderr)
            self.assertNotIn("DO_NOT_PRINT", result.stdout + result.stderr)
            self.assertIn("invalid_cli_arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
