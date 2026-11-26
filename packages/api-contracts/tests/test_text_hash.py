import json
import unicodedata

import pytest

from campuslife_contracts import Contract, contract_root, text_hash
from campuslife_contracts.fixtures import layout_problems
from campuslife_contracts.jsonio import read_strict
from campuslife_contracts.text_hash import discover_text_hash_vectors, text_hash_vector_problems

from conftest import write_schema


def test_hash_is_lowercase_sha256_hex_of_utf8():
    assert text_hash("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert text_hash("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.mark.parametrize(
    ("variant", "canonical"),
    [
        ("line\r\nnext", "line\nnext"),
        ("line\rnext", "line\nnext"),
        ("a\r\n\r\nb", "a\n\nb"),
        (unicodedata.normalize("NFD", "강의 노트"), "강의 노트"),
    ],
    ids=["crlf", "lone-cr", "blank-line-crlf", "nfd"],
)
def test_line_endings_and_normalization_do_not_change_the_hash(variant, canonical):
    assert text_hash(variant) == text_hash(canonical)


def test_different_text_hashes_differently():
    assert text_hash("line\nnext") != text_hash("line next")


def test_null_is_no_text_and_has_no_hash():
    with pytest.raises(TypeError):
        text_hash(None)


def put(root, relative, value):
    path = root / "fixtures" / "text_hash" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return path


EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_vectors_are_discovered_directly_under_text_hash(tmp_path):
    put(tmp_path, "empty.json", {"text": "", "hash": EMPTY})
    assert [vector.name for vector in discover_text_hash_vectors(tmp_path)] == ["empty"]


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ({"text": "", "hash": EMPTY}, None),
        ({"text": "", "hash": "0" * 64}, "text_hash gives e3b0c442"),
        ({"text": None, "hash": None}, "text must be a string"),
        ({"text": "", "hash": EMPTY.upper()}, "hash must be 64 lowercase hex digits"),
        ({"text": "", "hash": EMPTY, "note": "x"}, "members must be exactly hash, text"),
        ([], "a vector is a JSON object"),
        ('{"text": "", "text": "", "hash": "x"}', "not strict JSON"),
    ],
    ids=["good", "wrong-hash", "null-text", "uppercase-hash", "extra-member", "not-object", "duplicate-key"],
)
def test_vector_problems(tmp_path, value, problem):
    put(tmp_path, "case.json", value)
    (vector,) = discover_text_hash_vectors(tmp_path)
    problems = text_hash_vector_problems(vector)
    if problem is None:
        assert problems == []
    else:
        assert len(problems) == 1 and problem in problems[0], problems


def test_layout_refuses_nested_and_non_json_vectors(tmp_path):
    write_schema(tmp_path, "t/a", {"type": "object"})
    for kind in ("valid", "invalid", "forward"):
        (tmp_path / "fixtures" / kind).mkdir(parents=True, exist_ok=True)
    put(tmp_path, "deeper/case.json", {"text": "", "hash": EMPTY})
    put(tmp_path, "notes.txt", "x")
    assert layout_problems(tmp_path, Contract.load(tmp_path)) == [
        "fixtures/text_hash/deeper: vectors sit directly under text_hash",
        "fixtures/text_hash/notes.txt: not a .json file",
    ]


def vector(name):
    return read_strict(contract_root() / "fixtures" / "text_hash" / f"{name}.json")


@pytest.mark.parametrize(
    ("variants", "canonical"),
    [
        (["crlf", "cr", "mixed_line_endings"], "lf"),
        (["nfd"], "nfc"),
        (["combining_acute"], "precomposed_acute"),
        (["composition_exclusion"], "composition_exclusion_decomposed"),
        (["angstrom_sign"], "a_with_ring"),
        (["lf_cr"], "lf_lf"),
    ],
    ids=["line-endings", "hangul", "combining", "composition-exclusion", "singleton", "lf-then-cr"],
)
def test_golden_vectors_of_the_same_text_share_a_hash(variants, canonical):
    assert {vector(name)["hash"] for name in variants} == {vector(canonical)["hash"]}


def test_golden_vectors_include_the_known_literals():
    assert vector("empty") == {"text": "", "hash": EMPTY}
    assert vector("non_bmp")["text"] == "\U0001F4DA 강의"


@pytest.mark.parametrize(
    ("name", "other"),
    [("ligature_fi", "letters_fi"), ("line_separator", "lf"), ("next_line", "lf")],
    ids=["nfc-not-nfkc", "u2028-is-not-a-line-ending", "nel-is-not-a-line-ending"],
)
def test_golden_vectors_that_only_look_alike_hash_differently(name, other):
    assert vector(name)["hash"] != vector(other)["hash"]


def test_golden_vectors_keep_the_texts_they_test():
    assert vector("composition_exclusion")["text"] == "\u0958"
    assert vector("angstrom_sign")["text"] == "\u212b"
    assert vector("line_separator")["text"] == "a\u2028b"
    assert vector("next_line")["text"] == "a\u0085b"
