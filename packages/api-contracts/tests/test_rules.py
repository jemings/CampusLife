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
