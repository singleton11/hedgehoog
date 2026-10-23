"""Host-free regression tests; Docker is a shell mock, never the host daemon."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[2] / "deploy" / "install-release.sh"
A, B, C, D = (character * 40 for character in "abcd")


class ReleaseImageRetentionTests(unittest.TestCase):
    def run_shell(self, commands, images="", used="", fail_list=False):
        mock = r'''
source "$INSTALLER"
docker() {
    case "$*" in
        'container ls --all --quiet')
            [[ -z "$USED" ]] || printf '%s\n' container1 ;;
        "container inspect --format {{.Image}} container1") printf '%s\n' "$USED" ;;
        'image ls --no-trunc --format {{.Repository}}:{{.Tag}} {{.ID}}')
            [[ "$FAIL_LIST" != 1 ]] || return 1
            printf '%s\n' "$IMAGES" ;;
        'image rm '*) printf '%s\n' "$3" ;;
        *) printf 'Unexpected Docker operation: %s\n' "$*" >&2; return 99 ;;
    esac
}
'''
        with tempfile.TemporaryDirectory() as directory:
            return subprocess.run(
                ["bash", "-c", mock + commands], cwd=directory,
                env={**os.environ, "INSTALLER": str(INSTALLER), "IMAGES": images,
                     "USED": used, "FAIL_LIST": str(int(fail_list))},
                text=True, capture_output=True, check=False,
            )

    def test_superseded_tags_expire_after_multiple_releases(self):
        images = "\n".join(
            f"hedgehoog-{repo}:{sha} sha256:{repo}{sha}"
            for sha in (A, B, C, D) for repo in ("backend", "web")
        )
        result = self.run_shell(
            f'record_successful_release {A}\nrecord_successful_release {B}\n'
            f'record_successful_release {C}\nrecord_successful_release {C}\n'
            'cleanup_release_images "$(cat deployed-sha)" "$(cat rollback-sha)"',
            images=images,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # A is superseded; D models an unused image from a failed rollout.
        self.assertEqual(set(result.stdout.splitlines()), {
            f"hedgehoog-{repo}:{sha}" for sha in (A, D) for repo in ("backend", "web")
        })

    def test_container_images_and_unrelated_tags_are_never_removed(self):
        images = "\n".join([
            f"hedgehoog-backend:{A} sha256:used",
            f"hedgehoog-web:{A} sha256:unused",
            f"mongo:{A} sha256:mongo",
            "hedgehoog-web:latest sha256:latest",
            "hedgehoog-backend:<none> sha256:dangling",
        ])
        result = self.run_shell(f'cleanup_release_images {C} {B}', images, "sha256:used")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"hedgehoog-web:{A}"])

    def test_first_release_needs_no_rollback(self):
        result = self.run_shell(
            f'record_successful_release {A}\n'
            'test ! -e rollback-sha\n'
            'cleanup_release_images "$(cat deployed-sha)" ""',
            images=f"hedgehoog-web:{A} sha256:current",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_listing_failure_stops_cleanup(self):
        result = self.run_shell(f'cleanup_release_images {C} {B}', fail_list=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
