"""Rules owner and set_type (E5, E6, A3) on a small patch contract."""

import pytest

from campuslife_contracts import Rule, RuleViolation, SchemaViolation

ENTITY = {
    "type": "object",
    "required": ["id", "title", "fields"],
    "properties": {
        "id": {"type": "string", "x-owner": "client"},
        "title": {"type": "string", "minLength": 1, "x-owner": "user"},
        "body": {"type": "string", "x-owner": "user", "x-text": True},
        "due": {"$ref": "../common/day.json", "x-owner": "user"},
        "rev": {"type": "integer", "minimum": 0, "x-owner": "hub"},
        "kind": {"type": "string"},
        "fields": {"type": "object", "x-owner": "user"},
    },
}
DAY = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "x-calendar-date": True}
TASK = {"type": "object", "required": ["progress"], "properties": {"progress": {"enum": ["open", "done"]}, "note": {"type": "string"}}}
EXAM = {"type": "object", "required": ["note"], "properties": {"note": {"type": "integer"}, "room": {"type": "string"}}}
PATCH = {
    "type": "object",
    "required": ["entity", "set"],
    "properties": {
        "entity": {"enum": ["thing", "other"]},
        "set": {
            "type": "object",
            "propertyNames": {"pattern": "^([a-z][a-z_]*|fields\\.[a-z][a-z_]*)$"},
            "additionalProperties": {"type": "object", "properties": {"note": {"not": {}}}},
        },
    },
    "x-set-targets": {
        "thing": {"schema": "../t/thing.json", "fields": ["../t/task.json", "../t/exam.json"]},
        "other": {"schema": "../t/other.json"},
    },
    "x-set-owner": True,
    "x-set-type": True,
}
SCHEMAS = {
    "t/thing": ENTITY, "common/day": DAY, "t/task": TASK, "t/exam": EXAM,
    "t/other": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "x-owner": "user"},
            "mood": {"$ref": "../t/mood.json", "x-owner": "user"},
        },
    },
    "t/mood": {"type": "string", "pattern": "^[a-z]+$", "examples": ["calm", "busy"]},
    "t/patch": PATCH,
}


@pytest.fixture
def patch(make_contract):
    contract = make_contract(SCHEMAS)
    return lambda set_, entity="thing": contract.validate("t/patch", {"entity": entity, "set": set_})


def owner(path):
    return RuleViolation(path=path, rule=Rule.OWNER)


def set_type(path):
    return RuleViolation(path=path, rule=Rule.SET_TYPE)


def test_user_fields_with_values_of_their_type_pass(patch):
    assert patch({"title": {"from": "a", "to": "b"}, "due": {"from": None, "to": "2026-03-02"}}) == []
    assert patch({"fields.progress": {"from": "open", "to": "done"}}) == []


def test_writing_a_hub_owned_column_breaks_owner(patch):
    assert patch({"rev": {"from": 1, "to": 2}}) == [owner("/set/rev")]


def test_writing_a_column_the_creating_client_set_breaks_owner(patch):
    assert patch({"id": {"from": "a", "to": "b"}}) == [owner("/set/id")]


def test_writing_a_column_without_an_owner_breaks_owner(patch):
    assert patch({"kind": {"from": "a", "to": "b"}}) == [owner("/set/kind")]


def test_a_patch_writes_only_known_values_of_an_open_enum(patch):
    assert patch({"mood": {"from": "calm", "to": "busy"}}, entity="other") == []
    assert patch({"mood": {"from": "calm", "to": "sleepy"}}, entity="other") == [set_type("/set/mood/to")]
    assert patch({"mood": {"from": "sleepy", "to": "busy"}}, entity="other") == [set_type("/set/mood/from")]


def test_a_value_that_does_not_fit_the_column_breaks_set_type(patch):
    assert patch({"title": {"from": "a", "to": 3}}) == [set_type("/set/title/to")]
    assert patch({"title": {"from": 3, "to": "b"}}) == [set_type("/set/title/from")]
    assert patch({"due": {"from": None, "to": "2026-02-30"}}) == [set_type("/set/due/to")]


def test_an_unknown_column_breaks_set_type(patch):
    assert patch({"colour": {"from": None, "to": "x"}}) == [set_type("/set/colour")]
    assert patch({"title": {"from": "a", "to": "b"}}, entity="other") == [set_type("/set/title")]


def test_null_to_removes_an_optional_column_but_not_a_required_one(patch):
    assert patch({"due": {"from": "2026-03-02", "to": None}}) == []
    assert patch({"title": {"from": "a", "to": None}}) == [set_type("/set/title/to")]


def test_text_columns_compare_by_hash_and_others_by_value(patch):
    hash_ = "0" * 64
    assert patch({"body": {"from_hash": hash_, "to": "new"}}) == []
    assert patch({"body": {"from": "old", "to": "new"}}) == [set_type("/set/body/from")]
    assert patch({"title": {"from_hash": hash_, "to": "b"}}) == [set_type("/set/title/from_hash")]


def test_fields_keys_are_checked_against_every_kind_that_defines_them(patch):
    assert patch({"fields.note": {"from": None, "to": "text"}}) == []
    assert patch({"fields.note": {"from": None, "to": 3}}) == []
    assert patch({"fields.note": {"from": None, "to": True}}) == [set_type("/set/fields.note/to")]
    assert patch({"fields.progress": {"from": "open", "to": "later"}}) == [set_type("/set/fields.progress/to")]
    assert patch({"fields.colour": {"from": None, "to": "x"}}) == [set_type("/set/fields.colour")]


def test_null_to_is_refused_for_a_field_every_defining_kind_requires(patch):
    assert patch({"fields.progress": {"from": "open", "to": None}}) == [set_type("/set/fields.progress/to")]
    assert patch({"fields.room": {"from": "a", "to": None}}) == []


def test_null_to_is_allowed_for_a_field_some_defining_kind_has_optional(patch):
    assert patch({"fields.note": {"from": "a", "to": None}}) == []


def test_fields_keys_belong_to_entities_that_have_kind_fields(patch):
    assert patch({"fields.name": {"from": None, "to": "x"}}, entity="other") == [set_type("/set/fields.name")]


def test_rules_skip_what_the_schema_already_refuses(patch):
    assert patch({"Title": {"from": "a", "to": "b"}}) == [SchemaViolation("/set", "pattern")]
    assert patch({"title": "b"}) == [SchemaViolation("/set/title", "type")]
    assert patch({"title": {"from": 1, "to": 2, "note": "x"}}) == [SchemaViolation("/set/title/note", "not")]
    assert patch({"title": {"from": "a", "to": "b"}}, entity="nothing") == [SchemaViolation("/entity", "enum")]
