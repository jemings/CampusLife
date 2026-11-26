import json
from pathlib import Path

import pytest

from campuslife_contracts import Contract

SCHEMA_BASE = "https://campuslife.invalid/contracts/schemas/"


def write_schema(root: Path, schema_id: str, body: dict) -> None:
    path = root / "schemas" / f"{schema_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{SCHEMA_BASE}{schema_id}.json",
        **body,
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def make_contract(tmp_path):
    """Build a throwaway contract from {schema_id: schema body}."""

    def build(schemas: dict[str, dict]) -> Contract:
        for schema_id, body in schemas.items():
            write_schema(tmp_path, schema_id, body)
        return Contract.load(tmp_path)

    return build
