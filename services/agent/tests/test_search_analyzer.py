"""The Korean analyzer and the query plan, searched through a real FTS5 table built here.

Every document is synthetic. The table holds what the index will hold: the analyzed text in
an FTS5 column under the analyzer's tokenizer, and the NFC original beside it for scans.
"""

import sqlite3
import unicodedata

import pytest

from campuslife_hub.search.analyzer import (
    HANGUL_BIGRAM,
    LIKE_ESCAPE,
    MAX_QUERY_CHARS,
    MAX_QUERY_TERMS,
    QueryTooLong,
    plan_query,
)

DOCS = {
    1: "자료구조를 공부합니다. 이진탐색트리의 노드를 삽입합니다.",
    2: "과제 제출 마감은 금요일입니다.",
    3: "Python과제는 heap 정렬로 풉니다.",
    4: "힙 정렬과 큐를 비교합니다.",
    5: "중간고사 범위는 그래프입니다.",
    6: "최소힙에 원소를 넣습니다.",
}


class Index:
    """An in-memory index shaped like the Hub's: analyzed text to match, the original to scan."""

    def __init__(self, docs: dict[int, str], analyzer=HANGUL_BIGRAM):
        self.analyzer = analyzer
        self.con = sqlite3.connect(":memory:")
        self.con.execute(f"CREATE VIRTUAL TABLE fts USING fts5(body, tokenize='{analyzer.fts5_tokenize}')")
        self.con.execute("CREATE TABLE original(id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
        for doc_id, text in docs.items():
            self.add(doc_id, text)

    def add(self, doc_id: int, text: str) -> None:
        self.con.execute("INSERT INTO fts(rowid, body) VALUES (?, ?)", (doc_id, self.analyzer.analyze(text)))
        self.con.execute("INSERT INTO original(id, text) VALUES (?, ?)", (doc_id, unicodedata.normalize("NFC", text)))

    def search(self, query: str) -> list[int]:
        plan = plan_query(query, self.analyzer)
        if plan is None:
            return []
        scans = "".join(f" AND original.text LIKE ? ESCAPE '{LIKE_ESCAPE}'" for _ in plan.like)
        if plan.match is None:
            sql = f"SELECT id FROM original WHERE 1{scans} ORDER BY id"
            return [row[0] for row in self.con.execute(sql, plan.like)]
        sql = f"SELECT fts.rowid FROM fts JOIN original ON original.id = fts.rowid WHERE fts MATCH ?{scans} ORDER BY rank"
        return sorted(row[0] for row in self.con.execute(sql, (plan.match, *plan.like)))


@pytest.fixture
def index():
    return Index(DOCS)


def test_a_two_syllable_word_is_found_with_a_particle_attached(index):
    assert index.search("과제") == [2, 3]
    assert index.search("자료구조") == [1]


def test_a_word_inside_a_compound_is_found(index):
    assert index.search("탐색트리") == [1]
    assert index.search("고사") == [5]


def test_letters_on_both_sides_of_a_space_are_not_one_word(index):
    assert index.search("를공") == []
    assert index.search("과제제출") == []


def test_chinese_and_japanese_runs_are_split_the_same_way():
    assert Index({1: "情報処理の試験", 2: "データ構造"}).search("処理") == [1]
    assert Index({1: "情報処理の試験", 2: "データ構造"}).search("ータ") == [2]


def test_the_analyzer_writes_overlapping_pairs_and_leaves_other_text_to_the_tokenizer():
    assert HANGUL_BIGRAM.id == "hangul-bigram@1"
    assert HANGUL_BIGRAM.analyze(nfd("자료구조를")).split() == ["자료", "료구", "구조", "조를"]
    assert HANGUL_BIGRAM.analyze("Python과제 힙").split() == ["Python", "과제", "힙"]


def test_a_one_syllable_query_scans_the_original_text_because_no_bigram_holds_it(index):
    assert index.search("힙") == [4, 6]
    assert index.search("힙 정렬") == [4]


def nfd(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    assert decomposed != text
    return decomposed


def test_an_nfd_file_name_from_an_apple_device_is_found_by_an_nfc_query():
    index = Index({7: nfd("강의자료_3주차.pdf")})
    assert index.search("강의자료") == [7]
    assert index.search("3주차") == [7]
    assert index.search("의") == [7]


def test_an_nfd_query_finds_nfc_text(index):
    assert index.search(nfd("자료구조")) == [1]
    assert index.search(nfd("힙")) == [4, 6]


def test_a_mixed_korean_and_english_query_needs_every_term(index):
    assert index.search("Python 과제") == [3]
    assert index.search("Python과제") == [3]
    assert index.search("HEAP 정렬") == [3]
    assert index.search("heap 중간고사") == []


@pytest.fixture
def operators():
    return Index(
        {
            1: "과제 OR 그래프",
            2: "과제와 그래프",
            3: "그래프 NOT 과제",
            4: "body: 과제",
            5: '별표 * 와 따옴표 " 기호, 100% 완료 a_b',
        }
    )


def test_fts_operators_in_a_query_are_searched_as_text(operators):
    assert operators.search("과제 OR 그래프") == [1]
    assert operators.search("그래프 NOT 과제") == [3]
    assert operators.search("body:과제") == [4]
    assert operators.search("과*") == []
    assert operators.search("NEAR(과제 그래프)") == []
    assert operators.search("^그래프") == [1, 2, 3]


def test_a_term_of_only_punctuation_is_scanned_for_as_written(operators):
    assert operators.search("*") == [5]
    assert operators.search('"') == [5]
    assert operators.search("별표 *") == [5]
    assert operators.search("100%") == [5]
    assert operators.search("%") == [5]
    assert operators.search("_") == [5]


HOSTILE = [
    '"', '""', '"""', "'", "*", "과제*", "*과제", "NEAR(", "NEAR(과제, 2)", "NEAR(과제 그래프, 0)",
    "body:", ":", "body:*", "{body}:과제", "-body:과제", "^", "^^", "(", ")", "((과제", "과제))",
    "AND", "OR", "NOT", "과제 AND", "AND OR NOT", "+", "-", "과제 + 그래프",
    "\x00", "과\x00제", "과제\x00", "\x1b[0m", "\ud800", "과\udfff제", "\u200b", "\ufeff과제",
    "%", "%%", "_", "\\", "a\\", "\\%", "' OR 1=1 --", "과제'); DROP TABLE fts; --",
]


@pytest.mark.parametrize("query", HOSTILE)
def test_no_query_makes_the_search_fail(operators, query):
    assert isinstance(operators.search(query), list)
    assert operators.search("그래프") == [1, 2, 3]


def test_control_characters_and_lone_surrogates_separate_terms(operators):
    assert operators.search("과제\x00그래프") == operators.search("과제 그래프") == [1, 2, 3]
    assert operators.search("과제\ud800그래프") == [1, 2, 3]


def test_a_query_at_the_limits_is_searched(index):
    assert index.search(" ".join(["과제"] * MAX_QUERY_TERMS)) == [2, 3]
    assert index.search("과제 " + "가" * (MAX_QUERY_CHARS - 3)) == []


def test_a_query_over_the_limits_is_refused_before_it_reaches_sqlite():
    with pytest.raises(QueryTooLong):
        plan_query(" ".join(["과제"] * (MAX_QUERY_TERMS + 1)), HANGUL_BIGRAM)
    with pytest.raises(QueryTooLong):
        plan_query("가" * (MAX_QUERY_CHARS + 1), HANGUL_BIGRAM)


def test_the_length_limit_counts_a_decomposed_query_as_its_nfc_form(index):
    assert index.search(nfd("자" * MAX_QUERY_CHARS)) == []


def test_a_query_with_nothing_to_search_for_has_no_plan():
    assert plan_query("", HANGUL_BIGRAM) is None
    assert plan_query(" \t\n\x00\u3000", HANGUL_BIGRAM) is None
