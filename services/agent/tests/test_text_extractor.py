import codecs
import time

import pytest

from campuslife_hub.extract import text
from campuslife_hub.extract.limits import Limits

MARKDOWN = """서문 문단

# 1장 트리
트리는 계층 구조입니다.
둘째 줄

## 1.1 이진 트리 ##

```
# 주석이지 제목이 아님

```

끝 문단

# 2장 그래프
#해시태그는 제목이 아님
#
"""


def extracted(tmp_path, data, syntax="markdown", limits=Limits()):
    path = tmp_path / "input"
    path.write_bytes(data)
    return text.extract(path, {"syntax": syntax}, limits)


def places(result):
    return [(block["block_type"], block["text"], block["refs"][0]["heading_path"], block["refs"][0]["block_index"]) for block in result.blocks]


def test_markdown_headings_make_the_doc_path_of_the_blocks_under_them(tmp_path):
    result = extracted(tmp_path, MARKDOWN.encode())

    assert places(result) == [
        ("paragraph", "서문 문단", [], 0),
        ("heading", "1장 트리", [], 1),
        ("paragraph", "트리는 계층 구조입니다.\n둘째 줄", ["1장 트리"], 0),
        ("heading", "1.1 이진 트리", ["1장 트리"], 1),
        ("paragraph", "```\n# 주석이지 제목이 아님\n\n```", ["1장 트리", "1.1 이진 트리"], 0),
        ("paragraph", "끝 문단", ["1장 트리", "1.1 이진 트리"], 1),
        ("heading", "2장 그래프", [], 2),
        ("paragraph", "#해시태그는 제목이 아님\n#", ["2장 그래프"], 0),
    ]
    assert [block.get("payload") for block in result.blocks if block["block_type"] == "heading"] == [{"level": 1}, {"level": 2}, {"level": 1}]
    assert all(block["refs"][0]["kind"] == "doc_path" for block in result.blocks)
    assert (result.warnings, result.partial) == ([], False)


@pytest.mark.parametrize(
    ("line", "heading"),
    [
        ("# 제목 #", "제목"), ("## 제목 ###   ", "제목"), ("# 제목#", "제목#"), ("# C# 언어", "C# 언어"),
        ("# a ## b ##", "a ## b"), ("#\t제목\t#", "제목"), ("# #", "#"),
    ],
)
def test_a_closing_sequence_of_hashes_after_a_blank_is_not_part_of_the_heading(tmp_path, line, heading):
    assert places(extracted(tmp_path, line.encode())) == [("heading", heading, [], 0)]


def test_a_heading_line_of_long_blanks_is_read_in_linear_time(tmp_path):
    """The former lazy heading pattern took 1.48 s on a line of 20,000 blanks and grew with their square (review
    measurement, local Linux), so 200,000 would take minutes; read in linear time the line takes 0.1 ms here. 1 s
    parts the two with room for a slow CI runner."""
    line = b"# x" + b" " * 200_000 + b"y"
    started = time.perf_counter()

    result = extracted(tmp_path, line)

    assert time.perf_counter() - started < 1.0
    assert result.blocks[0]["text"] == ("x" + " " * 200_000 + "y")[:Limits().max_block_text_chars]


def test_plain_text_has_no_headings(tmp_path):
    result = extracted(tmp_path, MARKDOWN.encode(), syntax="plain")

    assert {block["block_type"] for block in result.blocks} == {"paragraph"}
    assert result.blocks[1]["text"] == "# 1장 트리\n트리는 계층 구조입니다.\n둘째 줄"
    assert [block["refs"][0]["block_index"] for block in result.blocks] == list(range(len(result.blocks)))
    assert all(block["refs"][0]["heading_path"] == [] for block in result.blocks)


@pytest.mark.parametrize(
    "data",
    [
        "자료구조 노트".encode(),
        codecs.BOM_UTF8 + "자료구조 노트".encode(),
        codecs.BOM_UTF16_LE + "자료구조 노트".encode("utf-16-le"),
        codecs.BOM_UTF16_BE + "자료구조 노트".encode("utf-16-be"),
        "자료구조 노트".encode("cp949"),
    ],
    ids=["utf-8", "utf-8 bom", "utf-16 le", "utf-16 be", "cp949"],
)
def test_each_encoding_decodes_to_the_same_text(tmp_path, data):
    result = extracted(tmp_path, data, syntax="plain")

    assert [block["text"] for block in result.blocks] == ["자료구조 노트"]
    assert result.warnings == []


def test_bytes_no_encoding_decodes_are_replaced_with_a_warning(tmp_path):
    result = extracted(tmp_path, b"abc \xff\xfe\xfa def", syntax="plain")

    assert "�" in result.blocks[0]["text"]
    assert result.warnings == ["replaced_characters"]


def test_a_paragraph_longer_than_the_block_limit_is_cut(tmp_path):
    result = extracted(tmp_path, b"a" * 25, syntax="plain", limits=Limits(max_block_text_chars=10))

    assert [block["text"] for block in result.blocks] == ["a" * 10, "a" * 10, "a" * 5]
    assert [block["refs"][0]["block_index"] for block in result.blocks] == [0, 1, 2]


def test_past_the_block_limit_the_extraction_stops_partial(tmp_path):
    result = extracted(tmp_path, b"one\n\ntwo\n\nthree", syntax="plain", limits=Limits(max_blocks=2))

    assert [block["text"] for block in result.blocks] == ["one", "two"]
    assert (result.partial, result.warnings) == (True, ["limit_reached"])


def test_exactly_the_block_limit_is_whole(tmp_path):
    result = extracted(tmp_path, b"one\n\ntwo", syntax="plain", limits=Limits(max_blocks=2))

    assert (len(result.blocks), result.partial) == (2, False)


def test_an_empty_file_has_no_blocks(tmp_path):
    assert extracted(tmp_path, b"").blocks == []


def test_past_the_text_limit_the_extraction_stops_partial_with_the_text_cut_at_it(tmp_path):
    result = extracted(tmp_path, b"abcd\n\nefgh\n\nijkl", syntax="plain", limits=Limits(max_text_chars=6))

    assert [block["text"] for block in result.blocks] == ["abcd", "ef"]
    assert (result.partial, result.warnings) == (True, ["limit_reached"])


def test_exactly_the_text_limit_is_whole(tmp_path):
    result = extracted(tmp_path, b"abcd\n\nef", syntax="plain", limits=Limits(max_text_chars=6))

    assert ([block["text"] for block in result.blocks], result.partial) == (["abcd", "ef"], False)


def test_a_file_longer_than_the_text_limit_can_take_is_read_only_that_far(tmp_path):
    data = "가나다라".encode() * 4

    result = extracted(tmp_path, data, syntax="plain", limits=Limits(max_text_chars=3))

    assert [block["text"] for block in result.blocks] == ["가나다"]
    assert (result.partial, result.warnings) == (True, ["limit_reached"])


def test_a_character_cut_where_the_reading_stops_is_left_out_not_replaced(tmp_path):
    data = b"a" + b"\n" * 5 + "가나".encode()

    result = extracted(tmp_path, data, syntax="plain", limits=Limits(max_text_chars=2))

    assert [block["text"] for block in result.blocks] == ["a"]
    assert (result.partial, result.warnings) == (True, ["limit_reached"])


def test_a_markdown_heading_longer_than_a_block_is_cut(tmp_path):
    result = extracted(tmp_path, "# 아주 긴 제목입니다\n본문\n".encode(), limits=Limits(max_block_text_chars=6))

    assert places(result) == [("heading", "아주 긴 제", [], 0), ("paragraph", "본문", ["아주 긴 제"], 0)]
