"""The child's boundary (S3) and the ZIP gate (S8), read from the source."""

import ast
import sys
from pathlib import Path

import campuslife_hub
from campuslife_hub.extract import extractors, fakes

PACKAGE = Path(campuslife_hub.__file__).parent
CHILD_ROOTS = ("campuslife_hub.extract.child", *sorted({extractor.entry.partition(":")[0] for extractor in extractors.ALL}), fakes.ENTRY)
STDLIB_ALLOWED = frozenset({
    "__future__", "codecs", "collections", "contextlib", "dataclasses", "enum", "hashlib", "importlib", "io", "json", "os",
    "re", "stat", "sys", "time", "types", "unicodedata", "pathlib", "typing", "zipfile",
})
"""What the child may import besides its own modules: no sqlite3, socket, asyncio, subprocess or HTTP client."""


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


def child_closure() -> tuple[set[str], set[str]]:
    """The Hub modules the child loads, and the other modules they import."""
    seen, outside, todo = set(), set(), list(CHILD_ROOTS)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        parts = name.split(".")
        todo += [".".join(parts[:end]) for end in range(1, len(parts))]
        for imported in imports(name) if module_path(name).exists() else ():
            (todo.append(imported) if imported.startswith("campuslife_hub") else outside.add(imported.split(".")[0]))
    return seen, outside


def test_the_child_imports_only_extract_modules_and_the_allowed_standard_library():
    hub_modules, outside = child_closure()

    assert {name for name in hub_modules if not name.startswith("campuslife_hub.extract")} <= {"campuslife_hub"}
    assert outside <= STDLIB_ALLOWED
    assert {"campuslife_hub.extract.framework", "campuslife_hub.extract.running", "campuslife_hub.extract.bodies"}.isdisjoint(hub_modules)


def test_the_check_sees_a_forbidden_import(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "imports", lambda name: {"sqlite3"} if name == "campuslife_hub.extract.child" else set())

    assert "sqlite3" in child_closure()[1]


def test_only_safe_zip_imports_zipfile():
    importers = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
            if any(name and name.split(".")[0] == "zipfile" for name in names):
                importers.add(path.relative_to(PACKAGE).as_posix())

    assert importers == {"extract/safe_zip.py"}


def test_no_module_reaches_zipfile_by_name():
    """__import__ and importlib take the name as a string."""
    hits = [path for path in PACKAGE.rglob("*.py") if "'zipfile'" in path.read_text(encoding="utf-8") or '"zipfile"' in path.read_text(encoding="utf-8")]

    assert hits == []
