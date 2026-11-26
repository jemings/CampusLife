"""Conventions every bundled schema follows."""

from campuslife_contracts import contract_root
from campuslife_contracts.jsonio import read_strict

SCHEMAS = contract_root() / "schemas"


def integer_types(node, at=""):
    if isinstance(node, dict):
        if node.get("type") == "integer" or (isinstance(node.get("type"), list) and "integer" in node["type"]):
            yield at or "/"
        for name, value in node.items():
            yield from integer_types(value, f"{at}/{name}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from integer_types(value, f"{at}/{index}")


def test_every_integer_goes_through_int64():
    found = {
        f"{path.relative_to(SCHEMAS).as_posix()}#{at}"
        for path in SCHEMAS.rglob("*.json")
        for at in integer_types(read_strict(path))
    }
    assert found == {"common/int64.json#/"}
