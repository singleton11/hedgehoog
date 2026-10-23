"""Completion metadata for baseline measurements, never a test-quality gate."""

import json
import os
from pathlib import Path


class BaselineStatus:
    def __init__(self):
        self.calls = 0
        self.terminal = 0
        self.failures = 0
        self.errors = []

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append("collection")

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self.calls += 1
            self.terminal += 1
            self.failures += int(report.failed)
        elif report.when == "setup" and report.skipped:
            self.terminal += 1
        elif report.failed:
            self.errors.append(report.when)

    def pytest_internalerror(self, excrepr, excinfo):
        self.errors.append("internalerror")

    def pytest_keyboard_interrupt(self, excinfo):
        self.errors.append("keyboardinterrupt")

    def pytest_sessionfinish(self, session, exitstatus):
        tests = session.testscollected
        if exitstatus not in (0, 1):
            self.errors.append("exitstatus:%s" % exitstatus)
        if tests <= 0 or self.terminal != tests:
            self.errors.append("incomplete")
        path = os.environ.get("BASELINE_STATUS_FILE")
        if path:
            Path(path).write_text(json.dumps({
                "complete": not self.errors,
                "tests": tests,
                "failures": self.failures,
                "errors": self.errors,
            }) + "\n", encoding="utf-8")


def pytest_configure(config):
    config.pluginmanager.register(BaselineStatus(), "baseline-status")
