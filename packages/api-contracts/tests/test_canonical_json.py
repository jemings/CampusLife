from decimal import Decimal

import pytest

from campuslife_contracts import canonical_json
from campuslife_contracts.jsonio import loads_strict

from test_compare import DIFFERENT, DIFFERENT_IDS, EQUIVALENT, EQUIVALENT_IDS


def canonical_of(text, exact_numbers):
    return canonical_json(loads_strict(text, exact_numbers=exact_numbers))


@pytest.mark.parametrize("exact_numbers", [True, False], ids=["decimal", "float"])
@pytest.mark.parametrize(("left", "right"), EQUIVALENT, ids=EQUIVALENT_IDS)
def test_values_the_comparator_calls_equal_have_the_same_bytes(left, right, exact_numbers):
    assert canonical_of(left, exact_numbers) == canonical_of(right, exact_numbers)


@pytest.mark.parametrize(("left", "right", "where"), DIFFERENT, ids=DIFFERENT_IDS)
def test_values_the_comparator_calls_different_have_different_bytes(left, right, where):
    assert canonical_of(left, True) != canonical_of(right, True)


def test_bytes_are_sorted_compact_utf8_with_value_canonical_numbers():
    value = {"b": [1.0, None, True, Decimal("1.50"), -0.0], "a": "강\n", "c": {}}
    assert canonical_json(value) == '{"a":"강\\n","b":[1,null,true,1.5,0],"c":{}}'.encode()


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (Decimal("1e400"), "1e+400"),
        (Decimal("-2.5E-30"), "-2.5e-30"),
        (10**20, "100000000000000000000"),
        (2**53 + 1, "9007199254740993"),
        (0.000001, "0.000001"),
        (Decimal("0.0010"), "0.001"),
        (Decimal("12345678901234567890123456789012345"), "1.2345678901234567890123456789012345e+34"),
        (Decimal("1234567890.12345678901234567890123456789"), "1234567890.12345678901234567890123456789"),
        (Decimal("-1.500E+3"), "-1500"),
    ],
)
def test_number_form_depends_only_on_its_value(number, text):
    assert canonical_json(number) == text.encode()


@pytest.mark.parametrize("number", [float("nan"), float("inf"), Decimal("NaN"), Decimal("-Infinity")])
def test_non_finite_number_is_not_json(number):
    with pytest.raises(ValueError):
        canonical_json([number])


def test_number_beyond_double_range_is_not_strict_json_without_exact_numbers():
    from campuslife_contracts.jsonio import NotStrictJson

    with pytest.raises(NotStrictJson):
        loads_strict("[1e400]")
    assert canonical_json(loads_strict("[1e400]", exact_numbers=True)) == b"[1e+400]"


def test_integer_too_long_for_python_int_is_not_strict_json():
    from campuslife_contracts.jsonio import NotStrictJson

    digits = "1" + "0" * 5000
    with pytest.raises(NotStrictJson):
        loads_strict(digits)
    assert canonical_json(loads_strict(digits, exact_numbers=True)) == b"1e+5000"


@pytest.mark.parametrize("value", [{1: "a"}, {"a": {None: 1}}, [{("a",): 1}]])
def test_member_names_must_be_strings(value):
    with pytest.raises(TypeError):
        canonical_json(value)


def test_nesting_deeper_than_python_can_parse_is_not_strict_json():
    from campuslife_contracts.jsonio import NotStrictJson

    with pytest.raises(NotStrictJson):
        loads_strict("[" * 100_000 + "]" * 100_000)
