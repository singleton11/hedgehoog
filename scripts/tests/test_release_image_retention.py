"""Host-free release installer tests; Docker is a shell mock, never the host daemon."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[2] / "deploy" / "install-release.sh"
A, B, C, D = (character * 40 for character in "abcd")


class ReleaseInstallerTests(unittest.TestCase):
    def run_shell(self, commands, images="", used="", fail_list=False):
        mock = r'''
export HEDGEHOOG_ROOT="$PWD/runtime"
export HEDGEHOOG_STATIC_ROOT="$HEDGEHOOG_ROOT/static"
mkdir -p "$HEDGEHOOG_ROOT"
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

    def test_superseded_backend_and_all_legacy_web_tags_expire(self):
        images = "\n".join(
            f"hedgehoog-{repo}:{sha} sha256:{repo}{sha}"
            for sha in (A, B, C, D) for repo in ("backend", "web")
        )
        result = self.run_shell(
            f'record_successful_release {A}\nrecord_successful_release {B}\n'
            f'record_successful_release {C}\nrecord_successful_release {C}\n'
            'cleanup_release_images "$(cat "$HEDGEHOOG_ROOT/deployed-sha")" '
            '"$(cat "$HEDGEHOOG_ROOT/rollback-sha")"',
            images=images,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()), {
            f"hedgehoog-backend:{A}", f"hedgehoog-backend:{D}",
            *(f"hedgehoog-web:{sha}" for sha in (A, B, C, D)),
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
            'test ! -e "$HEDGEHOOG_ROOT/rollback-sha"\n'
            'cleanup_release_images "$(cat "$HEDGEHOOG_ROOT/deployed-sha")" ""',
            images=f"hedgehoog-backend:{A} sha256:current",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_listing_failure_stops_cleanup(self):
        result = self.run_shell(f'cleanup_release_images {C} {B}', fail_list=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_static_releases_activate_atomically_and_keep_one_rollback(self):
        result = self.run_shell(f'''
for sha in {A} {B} {C}; do
    mkdir -p "source-$sha/assets"
    printf '%s\n' "$sha" > "source-$sha/index.html"
    printf 'asset\n' > "source-$sha/assets/app.js"
    tar -C "source-$sha" -czf "$sha.tar.gz" .
    prepare_static_release "$sha" "$sha.tar.gz"
    activate_static_release "$sha"
    record_successful_release "$sha"
done
cleanup_static_releases {C} {B}
test "$(readlink "$HEDGEHOOG_STATIC_ROOT/current")" = "releases/{C}"
test ! -e "$HEDGEHOOG_STATIC_ROOT/releases/{A}"
test -f "$HEDGEHOOG_STATIC_ROOT/releases/{B}/index.html"
test -f "$HEDGEHOOG_STATIC_ROOT/releases/{C}/index.html"
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_static_release_requires_an_index(self):
        result = self.run_shell(f'''
mkdir empty
printf 'asset\n' > empty/app.js
tar -C empty -czf empty.tar.gz .
prepare_static_release {A} empty.tar.gz
''')
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
