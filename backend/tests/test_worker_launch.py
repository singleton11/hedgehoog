"""Exercise task-time imports through the configured worker launchers, not pytest's sys.path."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent


@pytest.mark.parametrize("launcher", ["compose", "compose-marketdata", "dockerfile"])
def test_worker_launcher_keeps_local_modules_importable_during_tasks(tmp_path, launcher):
    path, prefix = ((ROOT / "compose.yaml", "command: ") if launcher.startswith("compose")
                    else (BACKEND / "Dockerfile", "CMD "))
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip().startswith(prefix)]
    line = lines[1 if launcher == "compose-marketdata" else 0]
    command = json.loads(line[len(prefix):])
    if launcher == "compose":
        assert "--queues=etfs" in command
        assert "--concurrency=1" in command
    elif launcher == "compose-marketdata":
        assert "--queues=marketdata" in command
        assert "--concurrency=${MARKET_DATA_WORKER_CONCURRENCY:-16}" in command
    else:
        assert "--queues=etfs,marketdata" in command
    # Use the real launcher prefix, but an isolated memory broker and probe app.
    # Reverting either launcher to the celery console script must fail this test.
    launch = command[:command.index("-A")]
    launch[0] = str(Path(sys.executable).parent / launch[0])
    for source in BACKEND.glob("*.py"):
        shutil.copyfile(source, tmp_path / source.name)
    (tmp_path / "import_probe.py").write_text('''
import sys
from celery import signals
from celery_app import app

@app.task(name="probe.lazy_import")
def probe():
    try:
        assert "return_statistics" not in sys.modules
        from return_statistics import refresh_statistics
        assert callable(refresh_statistics)
        print("TASK_TIME_IMPORT_OK", flush=True)
    finally:
        app.control.shutdown()

@signals.worker_ready.connect
def enqueue_probe(**kwargs):
    probe.delay()
''')
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONSAFEPATH"}}
    env.update(CELERY_BROKER_URL="memory://", MARKETSTACK_API_KEY="", PYTHONUNBUFFERED="1")
    result = subprocess.run(
        [*launch, "-A", "import_probe:app", "worker", "--pool=solo", "--concurrency=1",
         "--loglevel=info", "--without-gossip", "--without-mingle", "--without-heartbeat"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Task probe.lazy_import[" in output, output
    assert "TASK_TIME_IMPORT_OK" in output, output
