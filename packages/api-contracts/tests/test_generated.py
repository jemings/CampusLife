"""Shape checks generated from the valid fixtures.

Each valid fixture is validated once while the `required`, `minLength`,
`maxLength` and `minItems` keywords record where they applied. Every recorded
constraint is then broken one at a time, and the result must be exactly that
one violation. The constraints observed this way must equal the tables below,
which are the contract written out independently of the schemas, so a
constraint dropped from a schema shows up as a table mismatch.
"""

from __future__ import annotations

import copy
from collections import defaultdict

import pytest

from campuslife_contracts import SchemaViolation, bundled_contract, contract_root
from campuslife_contracts.contract import schema_uri
from campuslife_contracts.dialect import ContractValidator
from campuslife_contracts.fixtures import FixtureKind, discover
from campuslife_contracts.jsonio import pointer, read_strict

CONTRACT = bundled_contract()
CASES = [case for case in discover(contract_root()) if case.kind is FixtureKind.VALID]
PROBED = ("required", "minLength", "maxLength", "minItems")
RULES_THAT_VALIDATE_VALUES = ("x-set-type", "x-set-owner")
"""They validate patch values against column schemas; those evaluations are not the patch schema's own."""
CONDITIONAL_KEYWORDS = {"if", "not", "anyOf", "oneOf"}


def conditional(location):
    """A constraint that holds only on some branch, or selects one: breaking it changes which branch applies.

    Selector schemas live in $defs named <thing>_is or <thing>_is_not and are reached from an if.
    """
    segments = location.split("#", 1)[1].split("/")
    return any(part in CONDITIONAL_KEYWORDS or part.endswith(("_is", "_is_not")) for part in segments)


def other_branch(schema, instance):
    """A union branch evaluated against an instance of another branch: a const member disagrees.

    oneOf evaluates every branch, so without this a branch's required members would be recorded for
    instances the discriminator sends elsewhere.
    """
    if not isinstance(instance, dict):
        return False
    members = schema.get("properties", {})
    return any(
        isinstance(member, dict) and "const" in member and name in instance and instance[name] != member["const"]
        for name, member in members.items()
    )


class TaggedText(str):
    """A string that remembers where it sits in the fixture."""


def tagged(node, at=(), where=None):
    if isinstance(node, dict):
        copy_ = {name: tagged(value, (*at, name), where) for name, value in node.items()}
    elif isinstance(node, list):
        copy_ = [tagged(value, (*at, index), where) for index, value in enumerate(node)]
    elif isinstance(node, str):
        copy_ = TaggedText(node)
        copy_.at = at
        return copy_
    else:
        return node
    where[id(copy_)] = at
    return copy_


def schema_locations():
    found = {}

    def walk(node, schema_id, at):
        if isinstance(node, dict):
            found[id(node)] = f"{schema_id}#{at}"
            for name, value in node.items():
                walk(value, schema_id, f"{at}/{name}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, schema_id, f"{at}/{index}")

    for schema_id in CONTRACT.schema_ids:
        walk(CONTRACT.registry.contents(schema_uri(schema_id)), schema_id, "")
    return found


def probe():
    """(schema_id, fixture instance, keyword, location, value, instance path) for every probed constraint."""
    locations = schema_locations()
    seen = set()
    records = []
    originals = {keyword: ContractValidator.VALIDATORS[keyword] for keyword in PROBED}

    def recorder(keyword):
        def check(validator, value, instance, schema):
            location = locations.get(id(schema))
            at = where.get(id(instance)) if isinstance(instance, (dict, list)) else getattr(instance, "at", None)
            if location is not None and at is not None and not conditional(location) and not other_branch(schema, instance):
                key = (case.schema_id, case.name, keyword, location, at)
                if key not in seen:
                    seen.add(key)
                    records.append((case, keyword, location, value, at))
            yield from originals[keyword](validator, value, instance, schema)

        return check

    originals.update({keyword: ContractValidator.VALIDATORS[keyword] for keyword in RULES_THAT_VALIDATE_VALUES})
    try:
        for keyword in PROBED:
            ContractValidator.VALIDATORS[keyword] = recorder(keyword)
        for keyword in RULES_THAT_VALIDATE_VALUES:
            ContractValidator.VALIDATORS[keyword] = lambda *arguments: iter(())
        for case in CASES:
            where = {}
            CONTRACT.validate(case.schema_id, tagged(read_strict(case.path), (), where))
    finally:
        ContractValidator.VALIDATORS.update(originals)
    return records


RECORDS = probe()


def node_at(instance, at):
    for part in at:
        instance = instance[part]
    return instance


def over_long(text, limit):
    """Strings one longer than limit, each repeating one of text's characters where it stands, the first one first.

    Repeating a character in place keeps most patterns, but which character depends on the pattern: the first
    one breaks a pattern that starts with a literal such as https://.
    """
    if not text:
        return ["a" * (limit + 1)]
    extra = limit + 1 - len(text)
    return [text[:index] + text[index] * extra + text[index:] for index in range(len(text))]


def replaced(case, at, replacement):
    instance = read_strict(case.path)
    if not at:
        return replacement, (), None
    node_at(instance, at[:-1])[at[-1]] = replacement
    return instance, at, None


def broken(case, keyword, value, at):
    """Ways to break the probed constraint in the fixture, each with the member path it is reported at.

    A length bound is broken once, or for maxLength in several ways of which one must break it alone.
    """
    instance = read_strict(case.path)
    if keyword == "required":
        return [(instance, at, member) for member in value if member in node_at(instance, at)]
    node = node_at(instance, at)
    if keyword in {"minLength", "minItems"} and value >= 1:
        replacements = [node[: value - 1]]
    elif keyword == "maxLength":
        replacements = over_long(node, value)
    else:
        return []
    return [replaced(case, at, replacement) for replacement in replacements]


def cases_for(keyword):
    return [record for record in RECORDS if record[1] == keyword]


ALLOWED_ALONGSIDE = {"anyOf"}
"""Cross-member keywords that may report at the parent too (commit: attachments, url or user_note)."""


@pytest.mark.parametrize(
    "record", cases_for("required"), ids=lambda r: f"{r[0].schema_id}/{r[0].name}@{pointer(list(r[4])) or '/'}"
)
def test_each_required_member_missing_is_exactly_one_required_violation(record):
    case, _, _, value, at = record
    for instance, at_, member in broken(case, "required", value, at):
        stripped = copy.deepcopy(instance)
        del node_at(stripped, at_)[member]
        reported = CONTRACT.validate(case.schema_id, stripped)
        expected = SchemaViolation(pointer([*at_, member]), "required")
        assert expected in reported, member
        extra = [v for v in reported if v != expected]
        assert all(getattr(v, "keyword", None) in ALLOWED_ALONGSIDE and v.path == pointer(list(at_)) for v in extra), (
            member, extra)


@pytest.mark.parametrize(
    "record",
    [r for r in RECORDS if r[1] != "required"],
    ids=lambda r: f"{r[0].schema_id}/{r[0].name}@{pointer(list(r[4])) or '/'}:{r[1]}",
)
def test_each_length_bound_broken_is_exactly_that_violation(record):
    case, keyword, _, value, at = record
    reported = [CONTRACT.validate(case.schema_id, instance) for instance, _, _ in broken(case, keyword, value, at)]
    assert not reported or [SchemaViolation(pointer(list(at)), keyword)] in reported, reported[0]


def observed():
    table = defaultdict(set)
    for _, keyword, location, value, _ in RECORDS:
        if keyword == "required":
            table[location] |= set(value)
        else:
            table[location].add((keyword, value))
    return {location: sorted(entries, key=str) for location, entries in table.items()}


def test_observed_constraints_are_the_contract_tables():
    expected = {location: sorted(members, key=str) for location, members in {**REQUIRED, **LENGTHS}.items()}
    assert observed() == expected


def test_every_unconditional_constraint_is_reached_by_a_valid_fixture():
    declared = {
        location
        for node_id, location in schema_locations().items()
        if not conditional(location)
        for node in [_node_by_id[node_id]]
        if any(keyword in node for keyword in PROBED)
    }
    assert declared == set(observed())


def _nodes_by_id():
    found = {}

    def walk(node):
        if isinstance(node, dict):
            found[id(node)] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for schema_id in CONTRACT.schema_ids:
        walk(CONTRACT.registry.contents(schema_uri(schema_id)))
    return found


_node_by_id = _nodes_by_id()

REQUIRED = {
    "capture/attachment#": {"id", "capture_id", "blob_sha256", "role", "cloud_policy"},
    "capture/attachment#/$defs/consistency/then": {"seq"},
    "capture/capture#": {"id", "device_id", "created_at", "origin"},
    "capture/commit#": {"id", "created_at", "origin", "links", "attachments"},
    "capture/commit#/properties/links/items": {"type", "to"},
    "capture/commit_attachment#": {"id", "blob_sha256", "role", "cloud_policy"},
    "capture/origin#": {"type"},
    "capture/timeline#": {"boot_id", "anchors", "events"},
    "capture/timeline#/$defs/anchor": {"wall_ms", "mono_ns"},
    "capture/timeline#/$defs/event": {"type", "mono_ns", "media_pos_ms"},
    "common/error#": {"code", "message"},
    "common/local_date#": {"date", "tz"},
    "common/local_time_rule#": {"weekdays", "start", "end", "tz", "from_date", "until_date"},
    "item/fields/assignment#": {"course_id"},
    "item/fields/exam#": {"course_id"},
    "item/fields/lecture_session#": {"course_id", "date"},
    "item/fields/task#": {"progress"},
    "item/fields/term#": {"year", "semester", "starts_on", "ends_on"},
    "item/item#": {"id", "kind", "schema_version", "title", "fields", "rev"},
    "relation/relation#": {"id", "from", "type", "to"},
    "common/error#/allOf/0/then": {"details"},
    "common/error#/$defs/incomplete_details": {"missing_chunks"},
    "device/pair_request#": {"secret", "platform", "device_name", "app_version"},
    "device/pair_response#": {"device_id", "token"},
    "device/pairing_offer#": {"hub_url", "hub_id", "secret"},
    "device/status#": {"app_version", "pending_upload_count", "pending_mutation_count", "open_conflict_count"},
    "blob/upload_request#": {"sha256", "size"},
    "blob/upload_session#": {"sha256", "size", "chunk_size", "received"},
    "blob/stored#": {"sha256", "size"},
    "blob/download_response_headers#": {"ETag", "Accept-Ranges"},
    "blob/range_not_satisfiable_headers#": {"Content-Range"},
    "sync/changes_query#": {"since"},
    "artifact/artifact#": {"id", "artifact_type", "subject_ref", "job_key", "pipeline_version", "input_hashes", "content_blob", "created_at"},
    "locator/locator#": {"v", "kind"},
    "locator/locator#/$defs/known": {"v", "kind"},
    "suggestion/suggestion#": {"id", "source_artifact_id", "action", "proposed", "evidence", "fingerprint", "state"},
    "suggestion/suggestion#/$defs/create_item": {"action", "proposed"},
    "suggestion/suggestion#/$defs/patch_item": {"action", "proposed"},
    "suggestion/suggestion#/$defs/add_relation": {"action", "proposed"},
    "suggestion/suggestion#/$defs/item_patch": {"entity", "id", "set"},
    "suggestion/suggestion#/$defs/known": {"action", "proposed"},
    "suggestion/suggestion#/$defs/item_proposal": {"kind", "title", "fields"},
    "suggestion/suggestion#/$defs/proposed_change": {"to"},
    "suggestion/suggestion#/$defs/proposed_change/else": {"from"},
    "locator/locator#/$defs/pdf_page": {"v", "kind", "blob", "page"},
    "locator/locator#/$defs/slide": {"v", "kind", "blob", "slide"},
    "locator/locator#/$defs/doc_path": {"v", "kind", "blob", "heading_path", "block_index"},
    "locator/locator#/$defs/sheet_range": {"v", "kind", "blob", "sheet", "a1"},
    "locator/locator#/$defs/media_time": {"v", "kind", "capture", "start_ms", "end_ms"},
    "locator/locator#/$defs/image_region": {"v", "kind", "blob", "box"},
    "locator/locator#/$defs/web_snapshot": {"v", "kind", "blob", "url"},
    "locator/locator#/$defs/document": {"v", "kind", "blob"},
    "mutation/batch_request#": {"mutations"},
    "mutation/batch_response#": {"results"},
    "mutation/mutation#": {"mutation_id", "op"},
    "mutation/mutation#/$defs/capture_commit": {"mutation_id", "op", "capture"},
    "mutation/mutation#/$defs/change": {"to"},
    "mutation/mutation#/$defs/change/else": {"from"},
    "mutation/mutation#/$defs/create": {"mutation_id", "op", "entity", "id", "data"},
    "mutation/mutation#/$defs/create_item": {"mutation_id", "op", "entity", "id", "data"},
    "mutation/mutation#/$defs/create_relation": {"mutation_id", "op", "entity", "id", "data"},
    "mutation/mutation#/$defs/create_tag": {"mutation_id", "op", "entity", "id", "data"},
    "mutation/mutation#/$defs/delete": {"mutation_id", "op", "entity", "id"},
    "mutation/mutation#/$defs/item_data": {"kind", "title", "fields"},
    "mutation/mutation#/$defs/patch": {"mutation_id", "op", "entity", "id", "set"},
    "mutation/mutation#/$defs/relation_data": {"from", "type", "to"},
    "mutation/mutation#/$defs/restore": {"mutation_id", "op", "entity", "id"},
    "mutation/mutation#/$defs/suggestion_accept": {"mutation_id", "op", "suggestion_id"},
    "mutation/mutation#/$defs/suggestion_dismiss": {"mutation_id", "op", "suggestion_id"},
    "mutation/mutation#/$defs/tag_data": {"name", "color"},
    "mutation/result#": {"mutation_id", "result"},
    "mutation/result#/$defs/only_when/conflicted_fields/allOf/0/then": {"conflicted_fields"},
    "mutation/result#/$defs/only_when/missing_blobs/allOf/0/then": {"missing_blobs"},
    "mutation/result#/$defs/only_when/original/allOf/0/then": {"original"},
    "mutation/result#/$defs/only_when/reason/allOf/0/then": {"reason"},
    "sync/changes_page#": {"hub_id", "epoch", "changes", "next_since", "has_more"},
    "sync/changes_page#/$defs/change": {"entity", "id", "rev", "deleted"},
    "sync/changes_page#/$defs/change/allOf/1/then": {"data"},
    "sync/hub#": {"hub_id", "epoch", "contract_version", "min_contract_version"},
    "sync/request_headers#": {"CampusLife-Contract"},
    "sync/response_headers#": {"CampusLife-Hub-Id", "CampusLife-Epoch"},
    "tag/tag#": {"id", "name", "color"},
}

NON_EMPTY = {("minLength", 1)}
LENGTHS = {
    "capture/attachment#/properties/original_filename": NON_EMPTY,
    "capture/attachment#/properties/original_filename_raw": NON_EMPTY,
    "capture/capture#/properties/category_hint": NON_EMPTY,
    "capture/capture#/properties/user_note": NON_EMPTY,
    "capture/origin#/properties/app": NON_EMPTY,
    "capture/origin#/properties/distributed_by": NON_EMPTY,
    "capture/origin#/properties/publisher": NON_EMPTY,
    "capture/origin#/properties/uri": NON_EMPTY,
    "capture/timeline#/properties/anchors": {("minItems", 1)},
    "capture/timeline#/properties/boot_id": {("minLength", 1), ("maxLength", 64)},
    "capture/timeline#/properties/events": {("minItems", 1)},
    "common/box#": {("minItems", 4)},
    "common/local_time_rule#/properties/weekdays": {("minItems", 1)},
    "item/fields/assignment#/properties/due_text": NON_EMPTY,
    "item/fields/assignment#/properties/submission": NON_EMPTY,
    "item/fields/course#/properties/instructor": NON_EMPTY,
    "item/fields/course#/properties/room": NON_EMPTY,
    "item/fields/exam#/properties/scope_text": NON_EMPTY,
    "item/item#/properties/title": NON_EMPTY,
    "tag/tag#/properties/name": NON_EMPTY,
    "mutation/batch_request#/properties/mutations": {("minItems", 1)},
    "mutation/batch_response#/properties/results": {("minItems", 1)},
    "mutation/result#/properties/conflicted_fields": {("minItems", 1)},
    "mutation/result#/properties/missing_blobs": {("minItems", 1)},
    "sync/changes_page#/$defs/change/properties/id": NON_EMPTY,
    "common/error#/$defs/incomplete_details/properties/missing_chunks": {("minItems", 1)},
    "device/pair_request#/properties/app_version": {("minLength", 1), ("maxLength", 64)},
    "device/pair_request#/properties/device_name": {("minLength", 1), ("maxLength", 64)},
    "device/pair_response#/$defs/token": {("maxLength", 512)},
    "device/status#/properties/last_error": {("minLength", 1), ("maxLength", 1000)},
    "artifact/artifact#/$defs/name": {("minLength", 1), ("maxLength", 200)},
    "artifact/artifact#/properties/input_hashes": {("minItems", 1)},
    "suggestion/suggestion#/properties/conflicts_with": {("minItems", 1)},
    "relation/relation#/properties/locators": {("minItems", 1)},
    "locator/locator#/$defs/doc_path/properties/heading_path/items": NON_EMPTY,
    "locator/locator#/$defs/sheet_range/properties/sheet": {("minLength", 1), ("maxLength", 31)},
}
