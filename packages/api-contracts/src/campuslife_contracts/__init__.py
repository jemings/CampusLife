"""Reference validator for the CampusLife wire contract."""

from functools import cache
from importlib.resources import files
from pathlib import Path

from .contract import Contract, ContractError
from .jsonio import canonical_json
from .policy import effective_cloud_policy, resolve_cloud_policy
from .text_hash import text_hash
from .violations import Rule, RuleViolation, SchemaViolation, Violation


def contract_root() -> Path:
    """Directory holding `schemas/` and `fixtures/` for this installation.

    A wheel bundles them under the package; an editable install of the source
    tree finds them next to `src/`, because editable installs do not carry the
    wheel's force-included data.
    """
    bundled = Path(str(files(__package__))) / "contract"
    return bundled if bundled.is_dir() else Path(__file__).resolve().parents[2]


@cache
def bundled_contract() -> Contract:
    return Contract.load(contract_root())


def validate(schema_id: str, instance: object) -> list[Violation]:
    """Check one wire instance against a contract schema and its rules."""
    return bundled_contract().validate(schema_id, instance)


__all__ = [
    "Contract",
    "ContractError",
    "Rule",
    "RuleViolation",
    "SchemaViolation",
    "Violation",
    "bundled_contract",
    "canonical_json",
    "contract_root",
    "effective_cloud_policy",
    "resolve_cloud_policy",
    "text_hash",
    "validate",
]
