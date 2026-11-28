"""Suggestions (D10, E22): what a job proposes from an Artifact, and the user's decision on it.

Nothing a job proposes is user data. suggestion.accept writes the Item or Relation through
the same create and patch a client's Mutation goes through, and copies the evidence into
evidence Relations; suggestion.dismiss records the decision and nothing else. Each decision
moves an open Suggestion: a second decision, from another device say, finds it decided and
lost, as a conflict on state, except that dismissing again is already done.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import NamedTuple

from campuslife_contracts import SchemaViolation, canonical_json, validate

from . import refs, store
from .db import Tx
from .devices import DeviceId
from .outcomes import Applied, Conflict, Outcome, Reason, Rejected
from .store import Entity, EntityRef
from .user_data import FIELDS_PREFIX, ITEM_DATA, ITEM_KINDS, MUTATION_DEFS, TEXT_COLUMNS, create_item, create_relation, patch, with_value

DECIDED = frozenset({"accepted", "dismissed"})

LOCATOR_ANCHORS = (("blob", "blob:"), ("capture", "capture:"))
"""What a Locator rests on, in the order rule locator_anchor reads them."""

STATE_LOST = Conflict((), ("state",))

KIND_FIELDS = {kind: frozenset(closed["properties"]) for kind, closed in MUTATION_DEFS["closed_fields"].items()}

CHANGE_MEMBERS = tuple(MUTATION_DEFS["change"]["properties"])
"""from, from_hash and to: what a patch's set entry carries; a proposed one is open and may carry more."""

FINGERPRINT_TO_FILL = SchemaViolation("/fingerprint", "required")
"""A producer's proposal has no fingerprint: propose computes it (#9 decision 6)."""


class InvalidSuggestion(ValueError):
    """The proposal would break the contract, is of an action this Hub cannot accept, or names an Artifact or evidence
    the Hub does not hold."""


def _each_once(items: Iterable[dict]) -> list[dict]:
    kept: list[dict] = []
    for item in items:
        if item not in kept:
            kept.append(item)
    return kept


def _lf(value: object) -> object:
    return value.replace("\r\n", "\n").replace("\r", "\n") if isinstance(value, str) else value


def _with_lf_text(action: str, proposed: dict) -> dict:
    """An x-text value is kept with LF line ends (E7), and a model's text may carry CRs, so the Hub writes it with LF."""
    text = TEXT_COLUMNS[Entity.ITEM]
    if action == "create_item":
        return {name: _lf(value) if name in text else value for name, value in proposed.items()}
    if action == "patch_item":
        return {**proposed, "set": {key: {**change, "to": _lf(change["to"])} if key in text else change for key, change in proposed["set"].items()}}
    return proposed


def _title_key(title: str) -> str:
    """NFC, casefolded, without spaces or punctuation: a model that words the title a little otherwise proposes the same."""
    return "".join(
        char for char in unicodedata.normalize("NFC", title).casefold()
        if not (char.isspace() or unicodedata.category(char)[0] in "PZ")
    )


def _create_item_key(proposed: dict, evidence: list[dict]) -> list:
    date = proposed.get("due_at", proposed.get("starts_at"))
    return [proposed["kind"], sorted(_anchors(evidence)), date, _title_key(proposed["title"])]


def _patch_item_key(proposed: dict, evidence: list[dict]) -> list:
    return [proposed["id"], sorted([key, change["to"]] for key, change in proposed["set"].items())]


def _add_relation_key(proposed: dict, evidence: list[dict]) -> list:
    return [proposed["id"]]


def fingerprint(action: str, proposed: dict, evidence: list[dict]) -> str:
    """#9 decision 6: proposals with one fingerprint are the same one, so a decision on one holds for the other.
    Changing a key reopens every fingerprint the user decided."""
    return hashlib.sha256(canonical_json([action, *ACTIONS[action].key(proposed, evidence)])).hexdigest()


def _recorded_at(artifact: dict) -> datetime:
    return datetime.fromisoformat(artifact["created_at"])


def _recorded_no_earlier(tx: Tx, artifact_id: str, than: datetime) -> bool:
    artifact = store.get(tx, EntityRef(Entity.ARTIFACT, artifact_id))
    return artifact is not None and _recorded_at(artifact) >= than


def propose(
    tx: Tx, artifact_id: str, action: str, proposed: dict, evidence: Iterable[dict], *, conflicts_with: Iterable[str] = ()
) -> dict | None:
    """The open Suggestion of the proposal's fingerprint from the Artifact: a new one, or the one this Artifact proposed before.

    None when the user already accepted or dismissed that fingerprint, from any Artifact: it does not open again. None
    too when an Artifact recorded no earlier than this one has it open, so a job run again under an older pipeline does
    not push out a newer proposal. An open one from an older Artifact is superseded by the new one, so reprocessing
    leaves one open per fingerprint. Repeated evidence is kept once, and x-text with LF line ends.
    """
    draft = {"id": str(uuid.uuid4()), "source_artifact_id": artifact_id, "action": action, "proposed": proposed,
             "evidence": _each_once(evidence), "state": "open"}
    conflicts_with = list(conflicts_with)
    if conflicts_with:
        draft["conflicts_with"] = conflicts_with
    violations = [violation for violation in validate("suggestion/suggestion", draft) if violation != FINGERPRINT_TO_FILL]
    if violations:
        raise InvalidSuggestion(f"the proposal breaks the contract: {violations}")
    proposed = _with_lf_text(action, proposed)
    if accepted_write(tx, action, proposed, {}, draft["id"]) is None:
        raise InvalidSuggestion(f"the proposal cannot be accepted as proposed: no client could write it as a {action}")
    suggestion = {**draft, "proposed": proposed, "fingerprint": fingerprint(action, proposed, draft["evidence"])}
    artifact = store.get(tx, EntityRef(Entity.ARTIFACT, artifact_id))
    if artifact is None:
        raise InvalidSuggestion(f"no Artifact {artifact_id} is recorded")
    missing = [anchor for anchor in _anchors(suggestion["evidence"]) if not refs.exists(tx, anchor)]
    if missing:
        raise InvalidSuggestion(f"the evidence rests on what the Hub does not hold: {missing}")
    same = [
        json.loads(data)
        for (data,) in tx.execute("SELECT data FROM suggestions WHERE fingerprint = ? ORDER BY rowid", (suggestion["fingerprint"],))
    ]
    if any(row["state"] in DECIDED for row in same):
        return None
    still_open = [row for row in same if row["state"] == "open"]
    for row in still_open:
        if row["source_artifact_id"] == artifact_id:
            return row
    if any(_recorded_no_earlier(tx, row["source_artifact_id"], _recorded_at(artifact)) for row in still_open):
        return None
    for row in still_open:
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
    joined = _each_once([*current.get("locators", []), *locators])
    updated = {**current, "locators": joined} if joined else current
    if updated != stored:
        store.write(tx, ref, updated)


def _copy_evidence(tx: Tx, item_ref: str, anchors: dict[str, list[dict]]) -> None:
    for anchor, locators in anchors.items():
        relation_id = f"{item_ref}|evidence|{anchor}"
        _join_locators(tx, {"id": relation_id, "from": item_ref, "type": "evidence", "to": anchor}, locators)


def _create_item_write(tx: Tx, proposed: dict, edits: dict, mutation_id: str) -> dict | None:
    """The create a client would send with the proposed columns and the edits over them; the Hub picks the Item's id.
    An edit may add a column or a field the proposal left out (#9 decision 3): the create judges the result."""
    data = {name: proposed[name] for name in ITEM_DATA if name in proposed}
    for key, value in edits.items():
        data = with_value(data, key, value)
    if data["kind"] not in ITEM_KINDS:
        return None
    return {"mutation_id": mutation_id, "op": "create", "entity": "item", "id": str(uuid.uuid4()), "data": data}


def _patch_item_write(tx: Tx, proposed: dict, edits: dict, mutation_id: str) -> dict | None:
    """The proposed patch, an edit in place of its key's to. Only the members a patch's change has are kept.

    A field the Item's kind does not define can only be left out, never given a value: an Item keeps its kind
    (decision 4). An Item the Hub does not hold is left to the patch to answer."""
    if not edits.keys() <= proposed["set"].keys():
        return None
    changes = {
        key: {**{member: change[member] for member in CHANGE_MEMBERS if member in change}, **({"to": edits[key]} if key in edits else {})}
        for key, change in proposed["set"].items()
    }
    item = store.get(tx, EntityRef(Entity.ITEM, proposed["id"]))
    defined = KIND_FIELDS.get(item["kind"], frozenset()) if item else None
    if defined is not None and any(
        key.removeprefix(FIELDS_PREFIX) not in defined for key, change in changes.items() if key.startswith(FIELDS_PREFIX) and change["to"] is not None
    ):
        return None
    return {"mutation_id": mutation_id, "op": "patch", "entity": "item", "id": proposed["id"], "set": changes}


def _add_relation_write(tx: Tx, proposed: dict, edits: dict, mutation_id: str) -> dict | None:
    if edits:
        return None
    data = {name: proposed[name] for name in ("from", "type", "to")}
    return {"mutation_id": mutation_id, "op": "create", "entity": "relation", "id": proposed["id"], "data": data}


def accepted_write(tx: Tx, action: str, proposed: dict, edits: dict, mutation_id: str) -> dict | None:
    """The client Mutation accepting with edits writes, or None when it cannot be accepted so: the one "can be accepted"
    test propose (with no edits) and accept share (#9 decision 12). It must be one a client could send, closed fields
    included, of an action this Hub accepts. What is judged against rows that can still change (compare-and-set, link
    targets, a deleted Item) is left to the write."""
    write = ACTIONS[action].write(tx, proposed, edits, mutation_id) if action in ACTIONS else None
    if write is None or validate("mutation/mutation", write):
        return None
    return write


class Accepted(NamedTuple):
    """What accepting wrote, and the members the accepted Suggestion records beside its state."""

    outcome: Outcome
    recorded: dict


def _accept_create_item(tx: Tx, suggestion: dict, write: dict, device: DeviceId) -> Accepted:
    """The Suggestion records the Item it became (#9 decision 5)."""
    anchors = _anchors(suggestion["evidence"])
    if not all(refs.exists(tx, anchor) for anchor in anchors):
        return Accepted(Rejected(Reason.LINK_TARGET_MISSING), {})
    created = create_item(tx, EntityRef(Entity.ITEM, write["id"]), write["data"])
    if not isinstance(created, Applied):
        return Accepted(created, {})
    _copy_evidence(tx, f"item:{write['id']}", anchors)
    return Accepted(created, {"created_item_id": write["id"]})


def _accept_patch_item(tx: Tx, suggestion: dict, write: dict, device: DeviceId) -> Accepted:
    """Compare-and-set like a client's patch (D18); the evidence is added when at least one key was written."""
    anchors = _anchors(suggestion["evidence"])
    if not all(refs.exists(tx, anchor) for anchor in anchors):
        return Accepted(Rejected(Reason.LINK_TARGET_MISSING), {})
    patched = patch(tx, write, device)
    if isinstance(patched, Applied) or (isinstance(patched, Conflict) and patched.applied_fields):
        _copy_evidence(tx, f"item:{write['id']}", anchors)
    return Accepted(patched, {})


def _accept_add_relation(tx: Tx, suggestion: dict, write: dict, device: DeviceId) -> Accepted:
    """Only an evidence Relation carries Locators, those resting on its to (rule locator_anchor), so the evidence of
    another type stays on the Suggestion."""
    data = write["data"]
    created = create_relation(tx, EntityRef(Entity.RELATION, write["id"]), data)
    if isinstance(created, Applied) and data["type"] == "evidence":
        _join_locators(tx, {"id": write["id"], **data}, _anchors(suggestion["evidence"]).get(data["to"], []))
    return Accepted(created, {})


class Action(NamedTuple):
    key: Callable[[dict, list[dict]], list]
    """What fingerprint hashes."""
    write: Callable[[Tx, dict, dict, str], dict | None]
    """The client Mutation accepting with edits writes, or None when it is not one this action takes."""
    accept: Callable[[Tx, dict, dict, DeviceId], Accepted]
    """Writes that Mutation with the Suggestion's evidence."""


ACTIONS: dict[str, Action] = {
    "create_item": Action(_create_item_key, _create_item_write, _accept_create_item),
    "patch_item": Action(_patch_item_key, _patch_item_write, _accept_patch_item),
    "add_relation": Action(_add_relation_key, _add_relation_write, _accept_add_relation),
}
"""The actions this Hub proposes and accepts: the contract's known ones. A Suggestion of another is shown and dismissed only."""


def accept(tx: Tx, mutation: dict, device: DeviceId) -> Outcome:
    """Write what the Suggestion proposes and record it accepted; refused, it stays open and no synced row changes.

    A patch that lost compare-and-set still records the decision: the conflict says which keys lost.
    """
    suggestion = store.get(tx, EntityRef(Entity.SUGGESTION, mutation["suggestion_id"]))
    if suggestion is None:
        return Rejected(Reason.DELETED)
    if suggestion["state"] != "open":
        return STATE_LOST
    write = accepted_write(tx, suggestion["action"], suggestion["proposed"], mutation.get("edits", {}), mutation["mutation_id"])
    if write is None:
        return Rejected(Reason.SCHEMA)
    outcome, recorded = ACTIONS[suggestion["action"]].accept(tx, suggestion, write, device)
    if not isinstance(outcome, Rejected):
        store.write(tx, EntityRef(Entity.SUGGESTION, suggestion["id"]), {**suggestion, "state": "accepted", **recorded})
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
