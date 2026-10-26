"""Round-trip staged runtime patches without accessing the installed runtime."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


RUNTIME = Path(__file__).resolve().parents[1] / "automation-runtime"


class RuntimePatchTests(unittest.TestCase):
    def test_patches_round_trip_exact_recorded_baselines(self):
        manifest = json.loads((RUNTIME / "base-hashes.json").read_text())
        for name, entry in manifest.items():
            with self.subTest(component=name), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / name
                candidate = (RUNTIME / name).read_bytes()
                target.write_bytes(candidate)
                patch = RUNTIME / (name + ".patch")
                for flags in (("--reverse", "--check"), ("--reverse",)):
                    subprocess.run(
                        ["git", "apply", "--unidiff-zero", *flags, str(patch)],
                        cwd=directory, check=True, capture_output=True,
                    )
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), entry["sha256"])
                for flags in (("--check",), ()):
                    subprocess.run(
                        ["git", "apply", "--unidiff-zero", *flags, str(patch)],
                        cwd=directory, check=True, capture_output=True,
                    )
                self.assertEqual(target.read_bytes(), candidate)


if __name__ == "__main__":
    unittest.main()
