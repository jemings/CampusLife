"""Fail when secrets or personal data files are tracked by Git.

Usage: python3 tools/repo_guard.py [REPO_ROOT]
"""

from __future__ import annotations

import enum
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

FIXTURE_SIZE_LIMIT = 5 * 1024 * 1024

SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SECRET_DIRECTORIES = frozenset({"secrets", "credentials"})
ENV_EXAMPLE = ".env.example"
DATA_SUFFIXES = (
    ".sqlite", ".sqlite3", ".db", ".db-shm", ".db-wal",
    ".m4a", ".mp3", ".wav", ".mp4", ".mov",
    ".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp",
    ".hwp", ".hwpx", ".docx", ".pptx", ".xlsx",
)
EXAMPLES_DIRECTORY = "examples"
FIXTURES_DIRECTORY = "fixtures"
ANDROID_APP = ("apps", "android")
IPADOS_APP = ("apps", "ipados")
PROTOTYPE_ASSETS = ("apps", "prototype", "assets")
XCASSETS_SUFFIX = ".xcassets"


class ViolationKind(enum.Enum):
    SECRET_PATH = "비밀 경로가 추적됨"
    DATA_OUTSIDE_FIXTURES = "데이터·미디어 파일이 허용 경로(examples/, fixtures/, 앱 에셋) 밖에 있음"
    FIXTURE_TOO_LARGE = "fixture 파일이 크기 상한(5 MiB)을 넘음"


@dataclass(frozen=True)
class Violation:
    path: str
    kind: ViolationKind


def _is_secret(path: PurePosixPath) -> bool:
    name = path.name.lower()
    if name == ENV_EXAMPLE:
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name.endswith(SECRET_SUFFIXES):
        return True
    return any(part.lower() in SECRET_DIRECTORIES for part in path.parts[:-1])


def _is_android_resource(directories: tuple[str, ...]) -> bool:
    if directories[:2] != ANDROID_APP:
        return False
    return any(
        directory == "src" and directories[index + 2 : index + 3] == ("res",)
        for index, directory in enumerate(directories)
        if index >= len(ANDROID_APP)
    )


def _is_ipados_asset_catalog(directories: tuple[str, ...]) -> bool:
    return directories[:2] == IPADOS_APP and any(
        directory.endswith(XCASSETS_SUFFIX) for directory in directories[2:]
    )


def _is_data_allowed_path(path: PurePosixPath) -> bool:
    directories = path.parts[:-1]
    return bool(directories) and (
        directories[0] == EXAMPLES_DIRECTORY
        or FIXTURES_DIRECTORY in directories
        or _is_android_resource(directories)
        or _is_ipados_asset_catalog(directories)
        or directories[: len(PROTOTYPE_ASSETS)] == PROTOTYPE_ASSETS
    )


def display_path(path: str) -> str:
    """Make a path printable on one line, whatever its bytes or locale."""
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in path
    )


def find_violations(file_sizes: Mapping[str, int]) -> list[Violation]:
    """Classify tracked files (repo-relative POSIX path -> size in bytes)."""
    violations: list[Violation] = []
    for raw_path, size in file_sizes.items():
        path = PurePosixPath(raw_path)
        if _is_secret(path):
            violations.append(Violation(raw_path, ViolationKind.SECRET_PATH))
        elif not _is_data_allowed_path(path):
            if path.name.lower().endswith(DATA_SUFFIXES):
                violations.append(Violation(raw_path, ViolationKind.DATA_OUTSIDE_FIXTURES))
        elif size > FIXTURE_SIZE_LIMIT:
            violations.append(Violation(raw_path, ViolationKind.FIXTURE_TOO_LARGE))
    return violations


def read_tracked_file_sizes(repo_root: str) -> dict[str, int]:
    listing = subprocess.run(
        ["git", "-C", repo_root, "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    sizes: dict[str, int] = {}
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        path = os.fsdecode(raw)
        try:
            sizes[path] = os.lstat(os.path.join(repo_root, path)).st_size
        except FileNotFoundError:
            sizes[path] = 0
    return sizes


def main(argv: list[str]) -> int:
    default_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo_root = argv[0] if argv else default_root
    sys.stdout.reconfigure(errors="backslashreplace")
    violations = find_violations(read_tracked_file_sizes(repo_root))
    for violation in violations:
        print(f"{display_path(violation.path)}: {violation.kind.value}")
    if violations:
        print(f"repo guard: 위반 {len(violations)}건", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
