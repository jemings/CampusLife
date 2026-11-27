"""Command line: `campuslife-contracts compare <expected_dir> <actual_dir>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .compare import BrokenGolden, compare_dirs
from .contract import Contract, ContractError


def _directory(value: str) -> Path:
    path = Path(value)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"not a directory: {value}")
    return path


def _contract_of(expected_dir: Path) -> tuple[Contract | None, str]:
    """The contract whose `fixtures/valid` holds expected_dir, and expected_dir's path below it."""
    resolved = expected_dir.resolve()
    for valid in (resolved, *resolved.parents):
        if valid.name == "valid" and valid.parent.name == "fixtures":
            root = valid.parent.parent
            if not (root / "schemas").is_dir():
                print(f"integer spelling check off: no schemas/ next to {valid.parent}", file=sys.stderr)
                return None, ""
            below = resolved.relative_to(valid).as_posix()
            return Contract.load(root), "" if below == "." else below
    print(f"integer spelling check off: {resolved} is not under a fixtures/valid", file=sys.stderr)
    return None, ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="campuslife-contracts")
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser(
        "compare",
        help="compare a language harness's round-trip output with the golden fixtures",
        description=(
            "For every *.json under EXPECTED_DIR, read the file at the same relative path under "
            "ACTUAL_DIR and compare JSON values: numbers by exact decimal value, member order "
            "ignored, absent differs from null, strings by code point; when EXPECTED_DIR is a contract's fixtures/valid or below it, the Hub's validator also judges the output, so an integer place of the schema written with a fraction or exponent is a mismatch too. A missing output file is "
            "a mismatch; extra output files are ignored. Exit status: 0 all match, 1 mismatches "
            "(one per line on stdout), 2 usage error (an EXPECTED_DIR without *.json, or a file "
            "under it that is not strict JSON)."
        ),
    )
    compare.add_argument("expected_dir", type=_directory)
    compare.add_argument("actual_dir", type=_directory)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if not any(arguments.expected_dir.rglob("*.json")):
        parser.error(f"no *.json under {arguments.expected_dir}")
    sys.stdout.reconfigure(errors="backslashreplace")
    try:
        contract, schema_base = _contract_of(arguments.expected_dir)
        mismatches = compare_dirs(arguments.expected_dir, arguments.actual_dir, contract, schema_base)
    except ContractError as error:
        print(f"campuslife-contracts compare: broken contract: {error}", file=sys.stderr)
        return 2
    except BrokenGolden as error:
        print(f"campuslife-contracts compare: golden file is not strict JSON: {error}", file=sys.stderr)
        return 2
    for mismatch in mismatches:
        print(mismatch)
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
