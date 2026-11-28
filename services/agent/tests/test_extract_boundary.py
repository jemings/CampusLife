"""The child's boundary (S3) and the gates (S8 and the extractor issues' common decision), read from the source."""

import ast
import sys
from pathlib import Path

import pytest

import campuslife_hub
from campuslife_hub.extract import extractors, fakes

PACKAGE = Path(campuslife_hub.__file__).parent
CHILD_ROOTS = ("campuslife_hub.extract.child", *sorted({extractor.entry.partition(":")[0] for extractor in extractors.ALL}), fakes.ENTRY)
CHILD_ALLOWED = frozenset({
    "__future__", "codecs", "collections", "contextlib", "csv", "dataclasses", "enum", "hashlib", "importlib", "io", "json",
    "os", "re", "stat", "struct", "sys", "time", "types", "unicodedata", "pathlib", "typing", "xml", "zipfile", "zlib",
    "olefile",
})
"""What the child may import besides its own modules: no sqlite3, socket, asyncio or HTTP client. olefile is the one
library outside the standard one."""
GATES = {
    "zipfile": "extract/safe_zip.py",
    "xml": "extract/safe_xml.py",
    "olefile": "extract/cfb.py",
    "subprocess": "extract/office.py",
}
"""A module only its gate imports in the extractor package. subprocess is the converter's (#59): in the child, only
extract/office.py starts a program, soffice; extract/office.py is #59's to add. The Hub side outside the package uses
xml and subprocess for its own ends (autostart, the CLI)."""
GATED_IN_THE_WHOLE_HUB = frozenset({"zipfile", "olefile"})
"""Archives and compound files are untrusted input wherever they are opened."""


def module_path(name: str) -> Path:
    relative = Path(*name.split(".")[1:])
    package = PACKAGE / relative / "__init__.py"
    return package if package.exists() else (PACKAGE / relative).with_suffix(".py")


def imports(name: str) -> set[str]:
    """The modules name imports, absolute; a from-import of a package names each of its names as a module too."""
    tree = ast.parse(module_path(name).read_text(encoding="utf-8"))
    package = name if module_path(name).name == "__init__.py" else name.rpartition(".")[0]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
            found.add(base)
            found |= {f"{base}.{alias.name}" for alias in node.names if module_path(f"{base}.{alias.name}").exists()} if base.startswith("campuslife_hub") else set()
    return found


def child_closure() -> tuple[set[str], dict[str, set[str]]]:
    """The Hub modules the child loads, and each other top-level module they import with the Hub modules importing it."""
    seen, outside, todo = set(), {}, list(CHILD_ROOTS)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        parts = name.split(".")
        todo += [".".join(parts[:end]) for end in range(1, len(parts))]
        for imported in imports(name) if module_path(name).exists() else ():
            if imported.startswith("campuslife_hub"):
                todo.append(imported)
            else:
                outside.setdefault(imported.split(".")[0], set()).add(name)
    return seen, outside


def module_file(name: str) -> str:
    return module_path(name).relative_to(PACKAGE).as_posix()


def outside_the_rules(outside: dict[str, set[str]]) -> dict[str, set[str]]:
    """Each outside module the child may not import, or imports from a module other than its gate."""
    return {
        module: importers for module, importers in outside.items()
        if not (module in CHILD_ALLOWED or {module_file(importer) for importer in importers} <= {GATES.get(module)})
    }


def test_the_child_imports_only_extract_modules_and_the_allowed_modules():
    hub_modules, outside = child_closure()

    assert {name for name in hub_modules if not name.startswith("campuslife_hub.extract")} <= {"campuslife_hub"}
    assert outside_the_rules(outside) == {}
    assert {"campuslife_hub.extract.framework", "campuslife_hub.extract.running", "campuslife_hub.extract.bodies"}.isdisjoint(hub_modules)


def test_the_check_sees_a_forbidden_import(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "imports", lambda name: {"sqlite3"} if name == "campuslife_hub.extract.child" else set())

    assert outside_the_rules(child_closure()[1]) == {"sqlite3": {"campuslife_hub.extract.child"}}


def test_the_check_lets_the_child_start_a_program_from_the_office_gate_only():
    office = {"subprocess": {"campuslife_hub.extract.office"}}
    elsewhere = {"subprocess": {"campuslife_hub.extract.office", "campuslife_hub.extract.text"}}

    assert outside_the_rules(office) == {}
    assert outside_the_rules(elsewhere) == elsewhere


def scope(module: str) -> Path:
    return PACKAGE if module in GATED_IN_THE_WHOLE_HUB else PACKAGE / "extract"


def importers_of(module: str) -> set[str]:
    found = set()
    for path in scope(module).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
            if any(name and name.split(".")[0] == module for name in names):
                found.add(path.relative_to(PACKAGE).as_posix())
    return found


@pytest.mark.parametrize(("module", "gate"), sorted(GATES.items()))
def test_only_the_gate_imports_its_module(module, gate):
    """The gate itself may not exist yet (extract/office.py is #59's): then nothing imports the module."""
    assert importers_of(module) <= {gate}
    assert (PACKAGE / gate).exists() is (importers_of(module) == {gate})


@pytest.mark.parametrize("module", sorted(GATES))
def test_no_module_reaches_a_gated_module_by_name(module):
    """__import__ and importlib take the name as a string."""
    hits = [
        path for path in scope(module).rglob("*.py")
        if f"'{module}'" in path.read_text(encoding="utf-8") or f'"{module}"' in path.read_text(encoding="utf-8")
    ]

    assert hits == []
