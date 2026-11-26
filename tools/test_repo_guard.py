import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from repo_guard import (
    FIXTURE_SIZE_LIMIT,
    ViolationKind,
    display_path,
    find_violations,
)

GUARD = Path(__file__).with_name("repo_guard.py")


def kinds(files: dict[str, int]) -> dict[str, ViolationKind]:
    return {v.path: v.kind for v in find_violations(files)}


class SecretPathTest(unittest.TestCase):
    def test_env_files_are_violations(self):
        found = kinds({".env": 1, "apps/hub/.env.local": 1, ".env.production": 1})
        self.assertEqual(set(found), {".env", "apps/hub/.env.local", ".env.production"})
        self.assertEqual(set(found.values()), {ViolationKind.SECRET_PATH})

    def test_env_example_is_allowed(self):
        self.assertEqual(kinds({".env.example": 1, "apps/hub/.env.example": 1}), {})

    def test_env_lookalikes_are_allowed(self):
        self.assertEqual(kinds({"docs/environment.md": 1, "src/.envrc.md": 1}), {})

    def test_key_material_extensions_are_violations(self):
        paths = ["fake.pem", "a/b.key", "c.p12", "d.pfx", "E.PEM"]
        found = kinds({p: 1 for p in paths})
        self.assertEqual(set(found), set(paths))
        self.assertEqual(set(found.values()), {ViolationKind.SECRET_PATH})

    def test_secrets_and_credentials_directories_are_violations(self):
        paths = ["secrets/token.txt", "apps/hub/credentials/a.json", "x/secrets/y/z"]
        self.assertEqual(set(kinds({p: 1 for p in paths})), set(paths))

    def test_secret_path_is_a_violation_even_under_fixtures(self):
        self.assertEqual(
            kinds({"fixtures/fake.pem": 1}),
            {"fixtures/fake.pem": ViolationKind.SECRET_PATH},
        )

    def test_directory_names_that_merely_contain_secrets_are_allowed(self):
        self.assertEqual(kinds({"docs/secrets-policy.md": 1, "my_credentials/a.md": 1}), {})


class DataExtensionTest(unittest.TestCase):
    def test_every_ignored_extension_outside_allowed_paths_is_a_violation(self):
        extensions = [
            "sqlite", "sqlite3", "db", "db-shm", "db-wal",
            "m4a", "mp3", "wav", "mp4", "mov", "pdf", "jpg", "jpeg", "png",
            "heic", "webp", "hwp", "hwpx", "docx", "pptx", "xlsx",
        ]
        for ext in extensions:
            with self.subTest(ext=ext):
                path = f"docs/x.{ext}"
                self.assertEqual(kinds({path: 1}), {path: ViolationKind.DATA_OUTSIDE_FIXTURES})

    def test_uppercase_extension_is_a_violation(self):
        self.assertEqual(
            kinds({"docs/x.PDF": 1}), {"docs/x.PDF": ViolationKind.DATA_OUTSIDE_FIXTURES}
        )

    def test_examples_and_fixtures_allow_data(self):
        self.assertEqual(
            kinds({
                "examples/lecture.m4a": 1,
                "examples/deep/a.PDF": 1,
                "packages/api-contracts/fixtures/blob.png": 1,
                "fixtures/x.docx": 1,
            }),
            {},
        )

    def test_lookalike_directory_names_do_not_count_as_allowed(self):
        paths = ["my-fixtures/a.pdf", "fixtures.bak/a.pdf", "example/a.pdf", "docs/examples/a.pdf"]
        self.assertEqual(set(kinds({p: 1 for p in paths})), set(paths))

    def test_ordinary_files_are_allowed(self):
        self.assertEqual(kinds({"README.md": 10, "tools/a.py": 10, "docs/a.svg": 10}), {})


class AppAssetExceptionTest(unittest.TestCase):
    def test_android_res_under_any_source_set_is_allowed(self):
        self.assertEqual(
            kinds({
                "apps/android/app/src/main/res/drawable/a.png": 1,
                "apps/android/src/debug/res/a.webp": 1,
            }),
            {},
        )

    def test_android_png_outside_src_res_is_a_violation(self):
        paths = [
            "apps/android/app/res/a.png",
            "apps/android/app/src/main/a.png",
            "apps/android/app/src/main/assets/a.png",
            "apps/other/app/src/main/res/a.png",
        ]
        self.assertEqual(set(kinds({p: 1 for p in paths})), set(paths))

    def test_ipados_xcassets_is_allowed(self):
        self.assertEqual(kinds({"apps/ipados/X/Assets.xcassets/i.png": 1}), {})
        self.assertEqual(kinds({"apps/ipados/X/Assets.xcassets/Icon.appiconset/i.png": 1}), {})

    def test_ipados_png_outside_xcassets_is_a_violation(self):
        paths = ["apps/ipados/X/i.png", "apps/android/X/Assets.xcassets/i.png"]
        self.assertEqual(set(kinds({p: 1 for p in paths})), set(paths))

    def test_prototype_assets_is_allowed(self):
        self.assertEqual(kinds({"apps/prototype/assets/a.png": 1}), {})

    def test_prototype_png_outside_assets_is_a_violation(self):
        paths = ["apps/prototype/a.png", "apps/prototype/other/assets/a.png"]
        self.assertEqual(set(kinds({p: 1 for p in paths})), set(paths))

    def test_size_limit_applies_to_app_asset_exceptions(self):
        self.assertEqual(
            kinds({"apps/prototype/assets/a.png": FIXTURE_SIZE_LIMIT + 1}),
            {"apps/prototype/assets/a.png": ViolationKind.FIXTURE_TOO_LARGE},
        )


class MutationGuardTest(unittest.TestCase):
    def test_env_example_lookalikes_are_violations(self):
        paths = [".env.example.bak", ".env.example.local"]
        self.assertEqual(set(kinds({p: 1 for p in paths})), set(paths))

    def test_secret_names_are_case_insensitive(self):
        paths = ["Secrets/t.txt", ".ENV", "Credentials/a.json"]
        self.assertEqual(set(kinds({p: 1 for p in paths})), set(paths))

    def test_deeply_nested_fixture_is_allowed(self):
        self.assertEqual(kinds({"fixtures/a/b/c.png": 1}), {})

    def test_file_named_like_secret_directory_is_allowed(self):
        self.assertEqual(kinds({"docs/secrets": 1, "docs/credentials": 1}), {})


class DisplayPathTest(unittest.TestCase):
    def test_plain_path_is_unchanged(self):
        self.assertEqual(display_path("docs/한글.md"), "docs/한글.md")

    def test_newline_is_escaped(self):
        self.assertEqual(display_path("a\nb.pdf"), "a\\nb.pdf")

    def test_undecodable_byte_is_escaped(self):
        self.assertEqual(display_path(os.fsdecode(b"a\xff.pdf")), "a\\udcff.pdf")


class FixtureSizeTest(unittest.TestCase):
    def test_limit_is_five_mebibytes(self):
        self.assertEqual(FIXTURE_SIZE_LIMIT, 5 * 1024 * 1024)

    def test_exactly_at_limit_is_allowed(self):
        self.assertEqual(kinds({"fixtures/a.json": FIXTURE_SIZE_LIMIT}), {})

    def test_one_byte_over_is_a_violation(self):
        self.assertEqual(
            kinds({"examples/a.m4a": FIXTURE_SIZE_LIMIT + 1}),
            {"examples/a.m4a": ViolationKind.FIXTURE_TOO_LARGE},
        )

    def test_size_limit_does_not_apply_outside_allowed_paths(self):
        self.assertEqual(kinds({"docs/big.md": FIXTURE_SIZE_LIMIT * 10}), {})


class ReportingTest(unittest.TestCase):
    def test_all_violations_are_collected(self):
        found = kinds({"fake.pem": 1, "docs/x.pdf": 1, "README.md": 1})
        self.assertEqual(set(found), {"fake.pem", "docs/x.pdf"})

    def test_empty_tree_has_no_violations(self):
        self.assertEqual(find_violations({}), [])


class GuardProcessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True)

    def track(self, relative: bytes, content: bytes = b"x") -> None:
        target = Path(os.fsdecode(os.path.join(bytes(self.repo), relative)))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.git("add", "-f", "--", os.fsdecode(relative))

    def run_guard(self, env_extra=None) -> subprocess.CompletedProcess:
        env = {**os.environ, **(env_extra or {})}
        return subprocess.run(
            [sys.executable, str(GUARD), str(self.repo)],
            capture_output=True,
            env=env,
        )

    def test_clean_repository_exits_zero_without_output(self):
        self.track(b"README.md")
        self.track(b"fixtures/a.pdf")
        result = self.run_guard()
        self.assertEqual((result.returncode, result.stdout), (0, b""))

    def test_violations_exit_one_and_list_every_path(self):
        self.track(b"fake.pem")
        self.track(b"docs/x.pdf")
        self.track(b"README.md")
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        lines = result.stdout.decode().splitlines()
        self.assertEqual(sorted(line.split(":")[0] for line in lines), ["docs/x.pdf", "fake.pem"])

    def test_tracked_file_deleted_from_work_tree_is_still_judged(self):
        self.track(b"docs/gone.pdf")
        (self.repo / "docs/gone.pdf").unlink()
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"docs/gone.pdf", result.stdout)

    def test_deleted_fixture_counts_as_size_zero(self):
        self.track(b"fixtures/gone.pdf")
        (self.repo / "fixtures/gone.pdf").unlink()
        self.assertEqual(self.run_guard().returncode, 0)

    def test_hostile_names_print_one_line_each_under_ascii_locale(self):
        self.track(b"docs/a\xffb.pdf")
        self.track(b"docs/c\nd.pdf")
        self.track("docs/한글.pdf".encode())
        result = self.run_guard({"LC_ALL": "C", "PYTHONIOENCODING": "ascii"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(result.stdout.decode("ascii").splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
