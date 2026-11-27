"""Reference validator for the CampusLife wire contract."""

from functools import cache
from importlib.resources import files
from pathlib import Path

from .contract import Contract, ContractError
from .headers import contract_headers
from .jsonio import canonical_json
from .policy import effective_cloud_policy, resolve_cloud_policy
from .text_hash import text_hash
from .violations import Rule, RuleViolation, SchemaViolation, Violation

CONTRACT_VERSION = 1
"""The contract version this package describes: the Hub's contract_version in GET /v1/hub (E18)."""
MIN_CONTRACT_VERSION = 1
"""The oldest client contract version the Hub accepts; a request below it gets 400 contract_too_old (E18)."""
SENT_RETENTION_DAYS = 60
"""Days a client keeps a Mutation after the Hub acknowledged it, to send again in order when the Hub answers 409
epoch_changed (D24). Shorter than the Hub's 90 days of Mutation results, so a kept Mutation's first result is still
there and a resend of one the restored Hub holds gets duplicate. A capture.commit is kept while its originals are."""


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
    "CONTRACT_VERSION",
    "MIN_CONTRACT_VERSION",
    "Contract",
    "ContractError",
    "Rule",
    "RuleViolation",
    "SENT_RETENTION_DAYS",
    "SchemaViolation",
    "Violation",
    "bundled_contract",
    "canonical_json",
    "contract_headers",
    "contract_root",
    "effective_cloud_policy",
    "resolve_cloud_policy",
    "text_hash",
    "validate",
]
