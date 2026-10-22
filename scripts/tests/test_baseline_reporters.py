"""Small isolated runner fixtures; these do not run either application suite."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "backend/.venv/bin/python"
VITEST = ROOT / "frontend/node_modules/vitest/vitest.mjs"


class BaselineReportersTests(unittest.TestCase):
    def run_fixture(self, runner, source, extra=(), config_source=None, expect_status=True):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            status = directory / "status.json"
            env = os.environ.copy()
            env["BASELINE_STATUS_FILE"] = str(status)
            if runner == "pytest":
                if not PYTHON.exists():
                    self.skipTest("backend pytest environment not installed")
                (directory / "test_case.py").write_text(source, encoding="utf-8")
                env["PYTHONPATH"] = str(ROOT / "scripts")
                env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
                command = [str(PYTHON), "-m", "pytest", "-p", "baseline_pytest",
                           "-q", *extra, str(directory)]
            else:
                if not VITEST.exists() or not shutil.which("node"):
                    self.skipTest("Vitest not installed")
                # Mimic current/scripts beside a disposable baseline checkout:
                # only the measured checkout has installed dependencies.
                instrumentation = directory / "instrumentation"
                instrumentation.mkdir()
                for name in ("baseline_vitest.mjs", "baseline_vitest_runner.mjs"):
                    shutil.copyfile(ROOT / "scripts" / name, instrumentation / name)
                directory = directory / "checkout"
                directory.mkdir()
                (directory / "node_modules").symlink_to(ROOT / "frontend/node_modules", target_is_directory=True)
                (directory / "case.test.js").write_text(source, encoding="utf-8")
                (directory / "src").mkdir()
                shutil.copyfile(ROOT / "frontend/src/test-setup.js", directory / "src/test-setup.js")
                config = directory / "vitest.config.mjs"
                config.write_text(config_source or "export default {test: {globals: true}};", encoding="utf-8")
                command = ["node", str(VITEST), "run", "--root", str(directory),
                           "--config", str(config), "--reporter", "default",
                           "--reporter", str(instrumentation / "baseline_vitest.mjs"),
                           *extra]
            result = subprocess.run(command, cwd=directory, env=env, text=True,
                                    capture_output=True, timeout=45)
            if not expect_status:
                self.assertFalse(status.exists(), result.stdout + result.stderr)
                return result
            self.assertTrue(status.exists(), result.stdout + result.stderr)
            return json.loads(status.read_text()), result.returncode

    def test_pytest_completed_test_bodies(self):
        for body in ("pass", "assert False", "raise ValueError('body')"):
            with self.subTest(body=body):
                data, code = self.run_fixture("pytest", f"def test_body():\n    {body}\n")
                self.assertTrue(data["complete"], data)
                self.assertEqual(data["tests"], 1)
                self.assertEqual(data["failures"], int(body != "pass"))
                self.assertEqual(code, int(body != "pass"))

    def test_pytest_skips_count_as_terminal(self):
        data, code = self.run_fixture("pytest", "import pytest\n"
                                      "@pytest.mark.skip\ndef test_skip(): pass\n"
                                      "def test_dynamic(): pytest.skip('skip')\n")
        self.assertEqual(code, 0)
        self.assertTrue(data["complete"], data)
        self.assertEqual(data["tests"], 2)

    def test_pytest_rejects_non_body_errors(self):
        fixtures = {
            "collection": "raise ValueError('import')\n",
            "setup": "import pytest\n@pytest.fixture(autouse=True)\n"
                     "def fixture(): raise ValueError('setup')\ndef test_one(): pass\n",
            "teardown": "import pytest\n@pytest.fixture(autouse=True)\n"
                        "def fixture():\n    yield\n    raise ValueError('teardown')\n"
                        "def test_one(): pass\n",
            "interrupt": "def test_one(): raise KeyboardInterrupt()\n",
            "empty": "",
            "early-exit": "import pytest\ndef test_one(): pytest.exit('exit', returncode=1)\n",
        }
        for name, source in fixtures.items():
            with self.subTest(name=name):
                data, _ = self.run_fixture("pytest", source)
                self.assertFalse(data["complete"], data)
                self.assertTrue(data["errors"])
        data, _ = self.run_fixture("pytest", "def test_one(): assert False\n"
                                   "def test_two(): pass\n", ("-x",))
        self.assertFalse(data["complete"], data)

    def test_vitest_completed_test_bodies(self):
        for body in ("", "expect(1).toBe(2)", "throw new TypeError('body')"):
            with self.subTest(body=body):
                data, code = self.run_fixture("vitest", f"test('body', () => {{{body}}});\n"
                                               "test.skip('skip', () => {});\n"
                                               "test.todo('todo');\n")
                self.assertTrue(data["complete"], data)
                self.assertEqual(data["tests"], 3)
                self.assertEqual(data["failures"], int(bool(body)))
                self.assertEqual(code, int(bool(body)))

    def test_vitest_cleanup_failures_are_not_body_failures(self):
        for expression in ("new Error('cleanup')", "new TypeError('cleanup')", "'cleanup'", "[]"):
            for asynchronous in (False, True):
                callback = ("async " if asynchronous else "") + f"() => {{throw {expression}}}"
                fixtures = {
                    "returned": f"beforeEach(() => ({callback})); test('one', () => {{}});",
                    "finished-context": "test('one', ({onTestFinished}) => {"
                                        f"onTestFinished({callback});" + "});",
                    "finished-import": "import {onTestFinished} from 'vitest';"
                                       f"test('one', () => {{onTestFinished({callback});}});",
                    "failed-context": "test('one', ({onTestFailed}) => {"
                                      f"onTestFailed({callback}); throw new Error('body');" + "});",
                    "failed-import": "import {onTestFailed} from 'vitest';"
                                     f"test('one', () => {{onTestFailed({callback}); throw 42;}});",
                    "late-finished": f"afterEach(({{onTestFinished}}) => {{onTestFinished({callback});}});"
                                     "test('one', () => {});",
                }
                for name, source in fixtures.items():
                    with self.subTest(name=name, expression=expression, asynchronous=asynchronous):
                        data, code = self.run_fixture("vitest", source)
                        self.assertEqual(code, 1)
                        self.assertFalse(data["complete"], data)
                        self.assertTrue(any(error.startswith("cleanup:") for error in data["errors"]), data)

    def test_vitest_cleanup_failure_survives_retry_and_expected_failure(self):
        fixtures = {
            "returned": "let attempts = 0; beforeEach(() => () => {if (!attempts++) throw 'cleanup'});"
                        "test('one', () => {});",
            "finished": "let attempts = 0; test('one', ({onTestFinished}) => {"
                        "onTestFinished(() => {if (!attempts++) throw 'cleanup'});});",
            "failed": "let attempts = 0; test('one', ({onTestFailed}) => {"
                      "onTestFailed(() => {throw 'cleanup'}); if (!attempts++) throw 'body';});",
            "expected": "test.fails('one', ({onTestFinished}) => {"
                        "onTestFinished(() => {throw 'cleanup'});});",
            "fixture": "const custom = test.extend({item: async ({}, use) => {"
                       "await use(1); throw new Error('teardown')}});"
                       "custom.fails('one', ({item}) => {expect(item).toBe(1)});",
        }
        for name, source in fixtures.items():
            with self.subTest(name=name):
                data, _ = self.run_fixture("vitest", source, ("--retry", "1"))
                self.assertFalse(data["complete"], data)
                self.assertTrue(any(error.startswith("cleanup:") for error in data["errors"]), data)

    def test_vitest_successful_cleanups_and_arbitrary_body_errors(self):
        for body in ("", "throw new Error('body')", "throw new TypeError('body')",
                     "throw 'body'", "throw []", "expect.assertions(1)",
                     "expect.soft(1).toBe(2)"):
            with self.subTest(body=body):
                source = "beforeEach(async () => async () => {});" \
                         "test('one', ({onTestFinished, onTestFailed}) => {" \
                         "onTestFinished(async () => {}); onTestFailed(async () => {});" + body + "});"
                data, code = self.run_fixture("vitest", source)
                self.assertTrue(data["complete"], data)
                self.assertEqual(code, int(bool(body)))
        data, code = self.run_fixture("vitest", "test.fails('expected', () => {throw 'body'});"
                                      "test.fails('unexpected pass', () => {});")
        self.assertTrue(data["complete"], data)
        self.assertEqual(code, 1)

    def test_vitest_fixture_teardown_and_around_hooks(self):
        for failure in ("new Error('teardown')", "[]", "new AggregateError([])"):
            for body in ("", "throw 'body'"):
                for expected in (False, True):
                    with self.subTest(failure=failure, body=body, expected=expected):
                        source = "const custom = test.extend({item: async ({}, use) => {"
                        source += f"await use(1); throw {failure};" + "}});"
                        source += f"custom{'.fails' if expected else ''}('one', ({{item}}) => {{{body}}});"
                        data, _ = self.run_fixture("vitest", source)
                        self.assertFalse(data["complete"], data)
                        self.assertIn("cleanup:teardown", data["errors"])
        for name in ("aroundEach", "aroundAll"):
            data, _ = self.run_fixture("vitest", f"{name}(async (run) => {{await run(); throw []}});"
                                      "test('one', () => {});")
            self.assertFalse(data["complete"], data)
            self.assertTrue(any(error.startswith("cleanup:") for error in data["errors"]), data)

    def test_vitest_concurrent_cleanup_context_and_timeouts(self):
        data, code = self.run_fixture("vitest", "test.concurrent('one', async ({onTestFinished}) => {"
                                      "onTestFinished(async () => {throw 'cleanup'});"
                                      "await new Promise(resolve => setTimeout(resolve, 10));});"
                                      "test.concurrent('two', async ({onTestFinished}) => {"
                                      "onTestFinished(async () => {}); throw 'body';});")
        self.assertEqual(code, 1)
        self.assertFalse(data["complete"], data)
        self.assertIn("cleanup:onFinished", data["errors"])
        for hook in ("onTestFinished", "onTestFailed"):
            source = f"test('one', ({{{hook}}}) => {{{hook}(() => new Promise(() => {{}}), 10);"
            source += "throw 'body';});"
            with self.subTest(hook=hook):
                data, code = self.run_fixture("vitest", source)
                self.assertEqual(code, 1)
                self.assertFalse(data["complete"], data)
                self.assertTrue(any(error.startswith("cleanup:") for error in data["errors"]), data)

    def test_vitest_external_runner_with_real_vite_config(self):
        config = (ROOT / "frontend/vite.config.js").read_text()
        for source, complete in (
            ("import {test, expect} from 'vitest'; test('one', () => {expect(document).toBeDefined()});", True),
            ("import {test, beforeEach} from 'vitest'; beforeEach(() => () => {throw 'cleanup'});"
             "test('one', () => {});", False),
        ):
            with self.subTest(complete=complete):
                data, code = self.run_fixture("vitest", source, config_source=config)
                self.assertEqual(data["complete"], complete, data)
                self.assertEqual(data["tests"], 1, data)
                self.assertEqual(code, int(not complete))
                self.assertNotIn("unhandled", data["errors"], data)

    def test_vitest_custom_runner_fails_closed(self):
        result = self.run_fixture("vitest", "test('one', () => {});",
                                  config_source="export default {test: {globals: true, runner: './custom.mjs'}};",
                                  expect_status=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot replace a configured custom runner", result.stderr)

    def test_vitest_missing_cleanup_metadata_fails_closed(self):
        if not shutil.which("node"):
            self.skipTest("Node not installed")
        program = """
import Reporter from './scripts/baseline_vitest.mjs';
const test = {type: 'test', result: () => ({state: 'failed'})};
const module = {type: 'module', errors: () => [], state: () => 'failed', children: [test]};
new Reporter().onTestRunEnd([module], [], 'failed');
"""
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            subprocess.run(["node", "--input-type=module", "-e", program], cwd=ROOT,
                           env=dict(os.environ, BASELINE_STATUS_FILE=str(status)),
                           check=True, capture_output=True, timeout=10)
            data = json.loads(status.read_text())
            self.assertFalse(data["complete"], data)
            self.assertIn("cleanup:missing-instrumentation", data["errors"])

    def test_vitest_incomplete_and_interrupted_metadata(self):
        if not shutil.which("node"):
            self.skipTest("Node not installed")
        program = """
import Reporter from './scripts/baseline_vitest.mjs';
const reporter = new Reporter();
const state = process.env.CASE;
const test = {type: 'test', result: () => ({state})};
const module = {type: 'module', errors: () => [], state: () => 'passed', children: [test]};
reporter.onTestRunEnd([module], [], state === 'passed' ? 'interrupted' : 'failed');
"""
        for state in ("pending", "passed"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                status = Path(directory) / "status.json"
                env = dict(os.environ, CASE=state, BASELINE_STATUS_FILE=str(status))
                subprocess.run(["node", "--input-type=module", "-e", program],
                               cwd=ROOT, env=env, check=True, capture_output=True, timeout=10)
                data = json.loads(status.read_text())
                self.assertFalse(data["complete"], data)

    def test_vitest_rejects_non_body_errors(self):
        fixtures = {
            "collection": "throw new Error('import');",
            "suite": "describe('nested', () => {throw new Error('collect')});",
            "unhandled": "test('one', () => {Promise.reject(new Error('unhandled'))});",
            "empty": "",
        }
        for hook in ("beforeAll", "afterAll", "beforeEach", "afterEach"):
            fixtures[hook] = f"{hook}(() => {{throw new Error('hook')}}); test('one', () => {{}});"
        fixtures["retry-hook"] = ("let count = 0; beforeEach(() => {if (!count++) "
                                  "throw new Error('hook')}); test('one', () => {});")
        for name, source in fixtures.items():
            with self.subTest(name=name):
                extra = ("--retry", "1") if name == "retry-hook" else ()
                data, _ = self.run_fixture("vitest", source, extra)
                self.assertFalse(data["complete"], data)
                self.assertTrue(data["errors"])


if __name__ == "__main__":
    unittest.main()
