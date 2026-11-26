import os
import subprocess
import sys
import unicodedata

import pytest

from campuslife_contracts import Rule, RuleViolation, SchemaViolation

NFC = "강의 자료.pdf"
NFD = unicodedata.normalize("NFD", NFC)
OPEN_OBJECT = {"type": "object"}


def test_nfd_string_value_breaks_nfc(make_contract):
    contract = make_contract({"t/o": OPEN_OBJECT})
    assert contract.validate("t/o", {"name": NFC}) == []
    assert contract.validate("t/o", {"name": NFD, "list": ["ok", NFD]}) == [
        RuleViolation(path="/list/1", rule=Rule.NFC),
        RuleViolation(path="/name", rule=Rule.NFC),
    ]


def test_nfd_member_name_breaks_nfc_at_that_member(make_contract):
    contract = make_contract({"t/o": OPEN_OBJECT})
    assert contract.validate("t/o", {NFD: 1}) == [RuleViolation(path="/" + NFD, rule=Rule.NFC)]


def test_raw_members_keep_the_original_text(make_contract):
    contract = make_contract({"t/o": OPEN_OBJECT})
    assert contract.validate("t/o", {"original_filename": NFC, "original_filename_raw": NFD}) == []


def test_raw_member_name_itself_must_be_nfc(make_contract):
    contract = make_contract({"t/o": OPEN_OBJECT})
    key = unicodedata.normalize("NFD", "파일_raw")
    assert contract.validate("t/o", {key: NFD}) == [RuleViolation(path="/" + key, rule=Rule.NFC)]


def test_nfc_is_reported_alongside_schema_violations(make_contract):
    contract = make_contract({"t/s": {"type": "string", "maxLength": 3}})
    assert contract.validate("t/s", NFD) == [
        SchemaViolation(path="", keyword="maxLength"),
        RuleViolation(path="", rule=Rule.NFC),
    ]


def test_tz_must_exist_in_the_iana_database(make_contract):
    contract = make_contract({"t/tz": {"type": "string", "x-tz-exists": True}})
    assert contract.validate("t/tz", "Asia/Seoul") == []
    assert contract.validate("t/tz", "America/Argentina/Buenos_Aires") == []
    assert contract.validate("t/tz", "Asia/Gotham") == [RuleViolation(path="", rule=Rule.TZ_EXISTS)]


def test_tz_rule_ignores_values_of_another_type(make_contract):
    contract = make_contract({"t/tz": {"x-tz-exists": True}})
    assert contract.validate("t/tz", 9) == []


RANGES = {
    "type": "object",
    "x-range-order": [
        {"start": "start_ms", "end": "end_ms"},
        {"start": "start", "end": "end", "strict": True},
    ],
}


def test_range_end_may_equal_start_unless_strict(make_contract):
    contract = make_contract({"t/r": RANGES})
    assert contract.validate("t/r", {"start_ms": 5, "end_ms": 5}) == []
    assert contract.validate("t/r", {"start": "09:00", "end": "09:00"}) == [
        RuleViolation(path="/end", rule=Rule.RANGE_ORDER)
    ]


def test_range_end_before_start_breaks_range_order(make_contract):
    contract = make_contract({"t/r": RANGES})
    assert contract.validate("t/r", {"start_ms": 1930000, "end_ms": 1929999}) == [
        RuleViolation(path="/end_ms", rule=Rule.RANGE_ORDER)
    ]
    assert contract.validate("t/r", {"start": "10:30", "end": "09:00"}) == [
        RuleViolation(path="/end", rule=Rule.RANGE_ORDER)
    ]


def test_range_rule_skips_pairs_that_are_absent_or_not_comparable(make_contract):
    contract = make_contract({"t/r": RANGES})
    assert contract.validate("t/r", {"start_ms": 5}) == []
    assert contract.validate("t/r", {"start_ms": 5, "end_ms": "4"}) == []
    assert contract.validate("t/r", {"start_ms": True, "end_ms": 0}) == []


def test_box_must_stay_inside_the_unit_square(make_contract):
    contract = make_contract({"t/box": {"type": "array", "x-box-extent": True}})
    assert contract.validate("t/box", [0.5, 0.5, 0.5, 0.5]) == []
    assert contract.validate("t/box", [0.8, 0.2, 0.5, 0.3]) == [RuleViolation(path="", rule=Rule.BOX_EXTENT)]
    assert contract.validate("t/box", [0.2, 0.4, 0.3, 0.7]) == [RuleViolation(path="", rule=Rule.BOX_EXTENT)]


def test_box_rule_ignores_malformed_boxes(make_contract):
    contract = make_contract({"t/box": {"x-box-extent": True}})
    assert contract.validate("t/box", [0.8, 0.2]) == []
    assert contract.validate("t/box", ["a", 0, 1, 1]) == []


def test_tz_rule_holds_without_a_system_tz_database():
    # A fresh interpreter with an empty PYTHONTZPATH has no system zone files,
    # as on Windows; nothing cached by earlier tests can leak in.
    probe = (
        "import zoneinfo, campuslife_contracts as c;"
        "assert zoneinfo.TZPATH == ();"
        "print(c.validate('common/tz', 'Asia/Seoul'), c.validate('common/tz', 'Asia/Gotham'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env={**os.environ, "PYTHONTZPATH": ""},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "[] [RuleViolation(path='', rule=<Rule.TZ_EXISTS: 'tz_exists'>)]"


def test_tz_rule_skips_names_that_already_break_their_pattern(make_contract):
    contract = make_contract({"t/tz": {"type": "string", "pattern": "^[A-Z][a-z]+/[A-Za-z_]+$", "x-tz-exists": True}})
    assert contract.validate("t/tz", "+09:00") == [SchemaViolation(path="", keyword="pattern")]


def test_raw_exemption_covers_only_the_string_value_itself(make_contract):
    contract = make_contract({"t/o": OPEN_OBJECT})
    assert contract.validate("t/o", {"b_raw": {"nested": NFD}, "c_raw": [NFD]}) == [
        RuleViolation(path="/b_raw/nested", rule=Rule.NFC),
        RuleViolation(path="/c_raw/0", rule=Rule.NFC),
    ]


DATE = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}", "x-calendar-date": True}


def test_calendar_date_must_exist(make_contract):
    contract = make_contract({"t/date": DATE})
    assert contract.validate("t/date", "2024-02-29") == []
    assert contract.validate("t/date", "2024-02-29T23:59:59.999Z") == []
    assert contract.validate("t/date", "2023-02-29") == [RuleViolation(path="", rule=Rule.CALENDAR)]
    assert contract.validate("t/date", "2024-04-31") == [RuleViolation(path="", rule=Rule.CALENDAR)]
    assert contract.validate("t/date", "2023-02-30T00:00:00.000Z") == [RuleViolation(path="", rule=Rule.CALENDAR)]


def test_calendar_rule_skips_values_that_already_break_their_pattern(make_contract):
    contract = make_contract({"t/date": DATE})
    assert contract.validate("t/date", "2024-2-30") == [SchemaViolation(path="", keyword="pattern")]
    assert contract.validate("t/date", 20240229) == [SchemaViolation(path="", keyword="type")]


def test_range_rule_skips_pairs_whose_values_break_their_own_schema(make_contract):
    hhmm = {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"}
    contract = make_contract(
        {
            "t/slot": {
                "type": "object",
                "properties": {"start": hhmm, "end": hhmm},
                "x-range-order": [{"start": "start", "end": "end", "strict": True}],
            }
        }
    )
    assert contract.validate("t/slot", {"start": "24:00", "end": "10:15"}) == [
        SchemaViolation(path="/start", keyword="pattern")
    ]


def test_tz_rule_uses_exactly_the_tzdata_zone_list():
    from importlib.resources import files

    from campuslife_contracts.rules import _iana_zones

    shipped = set(files("tzdata").joinpath("zones").read_text(encoding="utf-8").split())
    assert _iana_zones() == shipped


RAW_FORM = {"type": "object", "x-raw-form": [{"raw": "name_raw", "text": "name"}]}


def test_raw_member_holds_the_received_form_of_its_nfc_text(make_contract):
    contract = make_contract({"t/file": RAW_FORM})
    assert contract.validate("t/file", {"name": NFC, "name_raw": NFD}) == []
    assert contract.validate("t/file", {"name": NFC}) == []


@pytest.mark.parametrize(
    "instance",
    [{"name": NFC, "name_raw": NFC}, {"name": NFC, "name_raw": unicodedata.normalize("NFD", "다른 파일.pdf")}],
    ids=["raw-already-nfc", "raw-of-another-name"],
)
def test_raw_member_that_is_not_the_received_form_breaks_raw_form(make_contract, instance):
    contract = make_contract({"t/file": RAW_FORM})
    assert contract.validate("t/file", instance) == [RuleViolation(path="/name_raw", rule=Rule.RAW_FORM)]


def test_raw_form_skips_members_that_break_their_own_schema(make_contract):
    contract = make_contract(
        {"t/file": {**RAW_FORM, "properties": {"name": {"type": "string", "minLength": 1}}}}
    )
    assert contract.validate("t/file", {"name": "", "name_raw": NFD}) == [
        SchemaViolation(path="/name", keyword="minLength")
    ]


RELATION = {
    "type": "object",
    "properties": {"type": {"enum": ["about", "tagged"]}},
    "x-relation-id": True,
}


def test_relation_id_is_from_type_to_joined_by_bars(make_contract):
    contract = make_contract({"t/rel": RELATION})
    good = {"id": "item:a|about|item:b", "from": "item:a", "type": "about", "to": "item:b"}
    assert contract.validate("t/rel", good) == []
    assert contract.validate("t/rel", {**good, "id": "item:b|about|item:a"}) == [
        RuleViolation(path="/id", rule=Rule.RELATION_ID)
    ]


def test_relation_id_rule_skips_members_that_break_their_own_schema(make_contract):
    contract = make_contract({"t/rel": RELATION})
    bad_type = {"id": "item:a|about|item:b", "from": "item:a", "type": "abuot", "to": "item:b"}
    assert contract.validate("t/rel", bad_type) == [SchemaViolation(path="/type", keyword="enum")]


SELF = {"type": "object", "properties": {"to": {"type": "string", "minLength": 1}}, "x-relation-not-self": True}


def test_relation_from_an_entity_to_itself_breaks_relation_self(make_contract):
    contract = make_contract({"t/rel": SELF})
    assert contract.validate("t/rel", {"from": "item:a", "to": "item:b"}) == []
    assert contract.validate("t/rel", {"from": "item:a", "to": "item:a"}) == [
        RuleViolation(path="/to", rule=Rule.RELATION_SELF)
    ]


def test_relation_self_skips_ends_that_break_their_own_schema(make_contract):
    contract = make_contract({"t/rel": SELF})
    assert contract.validate("t/rel", {"from": "", "to": ""}) == [SchemaViolation(path="/to", keyword="minLength")]


def test_relation_id_can_take_its_parts_from_a_member(make_contract):
    contract = make_contract({"t/create": {"type": "object", "x-relation-id": "data"}})
    good = {"id": "item:a|about|item:b", "data": {"from": "item:a", "type": "about", "to": "item:b"}}
    assert contract.validate("t/create", good) == []
    assert contract.validate("t/create", {**good, "id": "item:a|about|item:c"}) == [
        RuleViolation(path="/id", rule=Rule.RELATION_ID)
    ]


BATCH = {
    "type": "array",
    "items": {"type": "object", "properties": {"result": {"enum": ["applied", "retry", "not_attempted", "rejected"]}}},
    "x-batch-order": True,
}


@pytest.mark.parametrize(
    ("results", "broken"),
    [
        (["applied", "rejected", "applied"], []),
        (["applied", "retry", "not_attempted", "not_attempted"], []),
        (["retry"], []),
        (["applied", "retry", "applied"], [2]),
        (["not_attempted", "applied"], [0]),
        (["applied", "retry", "retry"], [2]),
    ],
    ids=["no-retry", "retry-then-rest-skipped", "lone-retry", "applied-after-retry", "skipped-without-retry",
         "second-retry"],
)
def test_only_mutations_after_the_first_retry_are_not_attempted(make_contract, results, broken):
    contract = make_contract({"t/batch": BATCH})
    instance = [{"result": result} for result in results]
    assert contract.validate("t/batch", instance) == [
        RuleViolation(path=f"/{index}/result", rule=Rule.BATCH_ORDER) for index in broken
    ]


def test_batch_order_skips_results_that_break_their_own_schema(make_contract):
    contract = make_contract({"t/batch": BATCH})
    assert contract.validate("t/batch", [{"result": "retry"}, {"result": "skipped"}]) == [
        SchemaViolation(path="/1/result", keyword="enum")
    ]


def test_batch_order_stops_judging_at_a_result_that_breaks_its_own_schema(make_contract):
    contract = make_contract({"t/batch": {**BATCH, "items": {**BATCH["items"], "required": ["result", "id"]}}})
    instance = [{"result": "applied", "id": 1}, {"result": "retry"}, {"result": "not_attempted", "id": 3}]
    assert contract.validate("t/batch", instance) == [SchemaViolation(path="/1/id", keyword="required")]


ORIGINAL = {
    "type": "object",
    "properties": {"mutation_id": {"type": "string", "pattern": "^m[0-9]$"}, "original": {"type": "object"}},
    "x-original-id": True,
}


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        ({"mutation_id": "m1", "original": {"mutation_id": "m1"}}, []),
        ({"mutation_id": "m1"}, []),
        ({"mutation_id": "m1", "original": {"mutation_id": "m2"}}, [RuleViolation("/original/mutation_id", Rule.ORIGINAL_ID)]),
        ({"mutation_id": "m1", "original": {}}, []),
    ],
    ids=["same", "no-original", "other-id", "original-without-id-is-required's"],
)
def test_original_id(make_contract, instance, expected):
    assert make_contract({"t/result": ORIGINAL}).validate("t/result", instance) == expected


def test_original_id_skips_ids_that_break_their_schema(make_contract):
    contract = make_contract({"t/result": ORIGINAL})
    assert contract.validate("t/result", {"mutation_id": "x", "original": {"mutation_id": "m2"}}) == [
        SchemaViolation("/mutation_id", "pattern")
    ]
    assert contract.validate("t/result", {"mutation_id": "m1", "original": {"mutation_id": "y"}}) == []


FEED = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "rev"],
                "properties": {"id": {"type": "string"}, "rev": {"type": "integer", "minimum": 0}, "data": {"type": "object"}},
            },
        },
        "next_since": {"type": "integer", "minimum": 0},
    },
    "x-feed-order": True,
}


def row(id_, rev, data_id=None):
    return {"id": id_, "rev": rev, **({} if data_id is None else {"data": {"id": data_id}})}


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        ({"changes": [row("a", 1, "a"), row("b", 4)], "next_since": 4}, []),
        ({"changes": [], "next_since": 0}, []),
        ({"changes": [row("a", 1, "b")], "next_since": 1}, ["/changes/0/id"]),
        ({"changes": [row("a", 3), row("b", 2), row("c", 4)], "next_since": 4}, ["/changes/1/rev"]),
        ({"changes": [row("a", 3), row("b", 3)], "next_since": 3}, ["/changes/1/rev"]),
        ({"changes": [row("a", 1), row("b", 5)], "next_since": 4}, ["/next_since"]),
    ],
    ids=["ordered", "empty", "id-differs-from-data", "rev-goes-back", "rev-repeats", "next-since-below-rev"],
)
def test_feed_order(make_contract, instance, expected):
    assert make_contract({"t/feed": FEED}).validate("t/feed", instance) == [
        RuleViolation(path, Rule.FEED_ORDER) for path in expected
    ]


def test_feed_order_skips_rows_and_cursors_that_break_their_schema(make_contract):
    contract = make_contract({"t/feed": FEED})
    assert contract.validate("t/feed", {"changes": [row("a", 3), row("b", -1), row("c", 4)], "next_since": 4}) == [
        SchemaViolation("/changes/1/rev", "minimum")
    ]
    assert contract.validate("t/feed", {"changes": [row("a", 3)], "next_since": "3"}) == [
        SchemaViolation("/next_since", "type")
    ]


CHUNKS = {
    "type": "object",
    "properties": {
        "size": {"type": "integer", "minimum": 0},
        "chunk_size": {"type": "integer", "minimum": 1},
        "received": {"type": "array", "items": {"type": "integer", "minimum": 0}},
    },
    "x-chunk-index": True,
}


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        ({"size": 9, "chunk_size": 4, "received": [0, 2, 1]}, []),
        ({"size": 9, "chunk_size": 4, "received": [3]}, ["/received/0"]),
        ({"size": 8, "chunk_size": 4, "received": [1, 2]}, ["/received/1"]),
        ({"size": 0, "chunk_size": 4, "received": []}, []),
        ({"size": 0, "chunk_size": 4, "received": [0]}, ["/received/0"]),
        ({"size": 1, "chunk_size": 4, "received": [0]}, []),
    ],
    ids=["inside", "past-the-last", "exact-multiple", "empty-blob", "chunk-of-an-empty-blob", "one-short-chunk"],
)
def test_received_chunks_are_below_the_chunk_count(make_contract, instance, expected):
    assert make_contract({"t/chunks": CHUNKS}).validate("t/chunks", instance) == [
        RuleViolation(path, Rule.CHUNK_INDEX) for path in expected
    ]


def test_chunk_index_skips_sessions_whose_members_break_their_schema(make_contract):
    contract = make_contract({"t/chunks": CHUNKS})
    assert contract.validate("t/chunks", {"size": 9, "chunk_size": 0, "received": [5]}) == [
        SchemaViolation("/chunk_size", "minimum")
    ]
    assert contract.validate("t/chunks", {"size": 9, "received": [5]}) == []
