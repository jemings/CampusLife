"""Suggestions (D10, E22): what a job proposes from an Artifact, and the user's decision on it.

Nothing a job proposes is user data. suggestion.accept writes the Item or Relation through
the same create and patch a client's Mutation goes through, and copies the evidence into
evidence Relations; suggestion.dismiss records the decision and nothing else. Each decision
moves an open Suggestion: a second decision, from another device say, finds it decided and
lost, as a conflict on state, except that dismissing again is already done.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable

from campuslife_contracts import contract_root, validate

from . import refs, store
from .db import Tx
from .devices import DeviceId
from .outcomes import Applied, Conflict, Outcome, Reason, Rejected
from .store import Entity, EntityRef
from .user_data import FIELDS_PREFIX, ITEM_DATA, could_be_created, create_item, create_relation, patch, with_value

_KNOWN = json.loads((contract_root() / "schemas" / "suggestion" / "suggestion.json").read_text(encoding="utf-8"))["$defs"]["known"]

KNOWN_ACTIONS = frozenset(_KNOWN["properties"]["action"]["enum"])
"""The actions this Hub proposes and accepts; a Suggestion of another action is shown and dismissed only."""

DECIDED = frozenset({"accepted", "dismissed"})

LOCATOR_ANCHORS = (("blob", "blob:"), ("capture", "capture:"))
"""What a Locator rests on, in the order rule locator_anchor reads them."""

STATE_LOST = Conflict((), ("state",))


class InvalidSuggestion(ValueError):
    """The proposal would break the contract, is of an action this Hub cannot accept, or names no recorded Artifact."""


def propose(
    tx: Tx, artifact_id: str, action: str, proposed: dict, evidence: Iterable[dict], fingerprint: str, *, conflicts_with: Iterable[str] = ()
) -> dict | None:
    """The open Suggestion of fingerprint from the Artifact: a new one, or the one this Artifact proposed before.

    None when the user already accepted or dismissed that fingerprint, from any Artifact: it does not open again.
    An open one from another Artifact is superseded by the new one, so reprocessing leaves one open per fingerprint.
    """
    suggestion = {
        "id": str(uuid.uuid4()),
        "source_artifact_id": artifact_id,
        "action": action,
        "proposed": proposed,
        "evidence": list(evidence),
        "fingerprint": fingerprint,
        "state": "open",
    }
    conflicts_with = list(conflicts_with)
    if conflicts_with:
        suggestion["conflicts_with"] = conflicts_with
    violations = validate("suggestion/suggestion", suggestion)
    if violations or action not in KNOWN_ACTIONS:
        raise InvalidSuggestion(f"this Hub cannot propose that {action}: {violations}")
    if store.get(tx, EntityRef(Entity.ARTIFACT, artifact_id)) is None:
        raise InvalidSuggestion(f"no Artifact {artifact_id} is recorded")
    same = [json.loads(data) for (data,) in tx.execute("SELECT data FROM suggestions WHERE fingerprint = ? ORDER BY rowid", (fingerprint,))]
    if any(row["state"] in DECIDED for row in same):
        return None
    for row in same:
        if row["state"] == "open" and row["source_artifact_id"] == artifact_id:
            return row
    for row in same:
        if row["state"] == "open":
            store.write(tx, EntityRef(Entity.SUGGESTION, row["id"]), {**row, "state": "superseded"})
    store.write(tx, EntityRef(Entity.SUGGESTION, suggestion["id"]), suggestion)
    return suggestion


def _anchors(evidence: list[dict]) -> dict[str, list[dict]]:
    """Each blob:<sha256> or capture:<id> the evidence rests on, with its Locators in evidence order."""
    anchors: dict[str, list[dict]] = {}
    for locator in evidence:
        anchor = next((f"{prefix}{locator[name]}" for name, prefix in LOCATOR_ANCHORS if isinstance(locator.get(name), str)), None)
        if anchor is not None:
            anchors.setdefault(anchor, []).append(locator)
    return anchors


def _join_locators(tx: Tx, relation: dict, locators: list[dict]) -> None:
    """Write the Relation with locators joined to those it has (D10): each kept once, those it had first."""
    ref = EntityRef(Entity.RELATION, relation["id"])
    stored = store.get(tx, ref)
    current = relation if stored is None else stored
    had = current.get("locators", [])
    joined = had + [locator for locator in locators if locator not in had]
    updated = {**current, "locators": joined} if joined else current
    if updated != stored:
        store.write(tx, ref, updated)


def _copy_evidence(tx: Tx, item_ref: str, anchors: dict[str, list[dict]]) -> None:
    for anchor, locators in anchors.items():
        relation_id = f"{item_ref}|evidence|{anchor}"
        _join_locators(tx, {"id": relation_id, "from": item_ref, "type": "evidence", "to": anchor}, locators)


def _proposes(proposed: dict, key: str) -> bool:
    if key.startswith(FIELDS_PREFIX):
        return key.removeprefix(FIELDS_PREFIX) in proposed["fields"]
    return key in proposed and key in ITEM_DATA


def _accept_create_item(tx: Tx, suggestion: dict, edits: dict, mutation: dict, device: DeviceId) -> Outcome:
    """The Item a client could create with the proposed columns, edits in their place; the Hub picks its id."""
    proposed = suggestion["proposed"]
    if not all(_proposes(proposed, key) for key in edits):
        return Rejected(Reason.SCHEMA)
    data = {name: proposed[name] for name in ITEM_DATA if name in proposed}
    for key, value in edits.items():
        data = with_value(data, key, value)
    anchors = _anchors(suggestion["evidence"])
    if not all(refs.exists(tx, anchor) for anchor in anchors):
        return Rejected(Reason.LINK_TARGET_MISSING)
    ref = EntityRef(Entity.ITEM, str(uuid.uuid4()))
    if not could_be_created(Entity.ITEM, {"id": ref.id, **data}, mutation["mutation_id"]):
        return Rejected(Reason.SCHEMA)
    created = create_item(tx, ref, data)
    if isinstance(created, Applied):
        _copy_evidence(tx, f"item:{ref.id}", anchors)
    return created


def _accept_patch_item(tx: Tx, suggestion: dict, edits: dict, mutation: dict, device: DeviceId) -> Outcome:
    """The proposed patch, an edit in place of its key's to, compare-and-set like a client's (D18)."""
    proposed = suggestion["proposed"]
    if not edits.keys() <= proposed["set"].keys():
        return Rejected(Reason.SCHEMA)
    changes = {key: {**change, "to": edits[key]} if key in edits else change for key, change in proposed["set"].items()}
    item_patch = {"mutation_id": mutation["mutation_id"], "op": "patch", "entity": "item", "id": proposed["id"], "set": changes}
    anchors = _anchors(suggestion["evidence"])
    if not all(refs.exists(tx, anchor) for anchor in anchors):
        return Rejected(Reason.LINK_TARGET_MISSING)
    patched = patch(tx, item_patch, device)
    if not isinstance(patched, Rejected):
        _copy_evidence(tx, f"item:{proposed['id']}", anchors)
    return patched


def _accept_add_relation(tx: Tx, suggestion: dict, edits: dict, mutation: dict, device: DeviceId) -> Outcome:
    """The proposed Relation as a client would create it. Only an evidence Relation carries Locators, those resting
    on its to (rule locator_anchor), so the evidence of another type stays on the Suggestion."""
    if edits:
        return Rejected(Reason.SCHEMA)
    proposed = suggestion["proposed"]
    data = {name: proposed[name] for name in ("from", "type", "to")}
    if validate("mutation/mutation", {"mutation_id": mutation["mutation_id"], "op": "create", "entity": "relation", "id": proposed["id"], "data": data}):
        return Rejected(Reason.SCHEMA)
    created = create_relation(tx, EntityRef(Entity.RELATION, proposed["id"]), data)
    if isinstance(created, Applied) and data["type"] == "evidence":
        _join_locators(tx, {"id": proposed["id"], **data}, _anchors(suggestion["evidence"]).get(data["to"], []))
    return created


ACCEPTS: dict[str, Callable[[Tx, dict, dict, dict, DeviceId], Outcome]] = {
    "create_item": _accept_create_item,
    "patch_item": _accept_patch_item,
    "add_relation": _accept_add_relation,
}


def accept(tx: Tx, mutation: dict, device: DeviceId) -> Outcome:
    """Write what the Suggestion proposes and record it accepted; refused, it stays open and no synced row changes.

    A patch that lost compare-and-set still records the decision: the conflict says which keys lost.
    """
    suggestion = store.get(tx, EntityRef(Entity.SUGGESTION, mutation["suggestion_id"]))
    if suggestion is None:
        return Rejected(Reason.DELETED)
    if suggestion["state"] != "open":
        return STATE_LOST
    accepting = ACCEPTS.get(suggestion["action"])
    if accepting is None:
        return Rejected(Reason.SCHEMA)
    outcome = accepting(tx, suggestion, mutation.get("edits", {}), mutation, device)
    if not isinstance(outcome, Rejected):
        store.write(tx, EntityRef(Entity.SUGGESTION, suggestion["id"]), {**suggestion, "state": "accepted"})
    return outcome


def dismiss(tx: Tx, mutation: dict, device: DeviceId) -> Outcome:
    suggestion = store.get(tx, EntityRef(Entity.SUGGESTION, mutation["suggestion_id"]))
    if suggestion is None:
        return Rejected(Reason.DELETED)
    if suggestion["state"] == "dismissed":
        return Applied()
    if suggestion["state"] != "open":
        return STATE_LOST
    store.write(tx, EntityRef(Entity.SUGGESTION, suggestion["id"]), {**suggestion, "state": "dismissed"})
    return Applied()
