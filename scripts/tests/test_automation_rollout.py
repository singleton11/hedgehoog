import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'check_automation_rollout.py'
spec = importlib.util.spec_from_file_location('rollout', SCRIPT)
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


class RolloutTests(unittest.TestCase):
    def test_missing_stale_matching_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, home = root / 'source', root / 'home'
            source.mkdir()
            home.mkdir()
            components = []
            for name in ('helper', 'reference', 'template'):
                (source / name).write_text('reviewed')
                components.append({'id': name, 'source': name, 'target': name})
            (home / 'reference').write_text('old')
            (home / 'template').write_text('reviewed')
            before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            report = rollout.check({'schema_version': 1, 'components': components}, source, home)
            self.assertEqual([c['state'] for c in report['components']], ['missing', 'stale', 'matching'])
            self.assertFalse(report['matching'])
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_source_missing_does_not_match_absent_target(self):
        result = rollout.check({'schema_version': 1, 'components': [
            {'id': 'x', 'source': 'missing', 'target': 'missing'}]}, Path('/nonexistent'), Path('/nonexistent'))
        self.assertEqual(result['components'][0]['state'], 'source-missing')
        self.assertFalse(result['matching'])

    def test_escape_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'escape').symlink_to('/etc')
            for path in ('../etc/passwd', '/etc/passwd', 'escape/passwd'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    rollout.bounded(root, path)

    def test_invalid_manifests_fail_closed(self):
        for manifest in ([], {}, {'schema_version': True},
                         {'schema_version': 1, 'components': []},
                         {'schema_version': 1, 'components': [None]},
                         {'schema_version': 1, 'components': [
                             {'id': 'x', 'source': 'a', 'target': 'a'},
                             {'id': 'x', 'source': 'a', 'target': 'a'}]}):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                rollout.check(manifest, Path('/nonexistent'), Path('/nonexistent'))

    def test_cli_exit_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'a').write_text('safe')
            manifest = root / 'manifest.json'
            data = {'schema_version': 1, 'components': [{'id': 'a', 'source': 'a', 'target': 'a'}]}
            manifest.write_text(json.dumps(data))
            args = [sys.executable, str(SCRIPT), '--manifest', str(manifest), '--source-root', temp, '--home', temp]
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(json.loads(result.stdout)['read_only'])
            data['components'][0]['target'] = 'absent'
            manifest.write_text(json.dumps(data))
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 1)
