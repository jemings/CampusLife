"""document.Outline, the place and limit rules every document extractor shares (#6, the extractor issues' common decision)."""

from campuslife_hub.extract.document import Outline
from campuslife_hub.extract.limits import Limits


def places(outline):
    return [
        (block["block_type"], block.get("text"), block["refs"][0].get("heading_path"), block["refs"][0].get("block_index"))
        for block in outline.result.blocks
    ]


def test_a_heading_closes_the_headings_of_its_level_and_deeper():
    outline = Outline(Limits())
    outline.heading(1, "1장")
    outline.heading(2, "1.1")
    outline.block("paragraph", "가")
    outline.heading(2, "1.2")
    outline.block("paragraph", "나")
    outline.heading(1, "2장")
    outline.block("table", "다\t라")

    assert places(outline) == [
        ("heading", "1장", [], 0),
        ("heading", "1.1", ["1장"], 0),
        ("paragraph", "가", ["1장", "1.1"], 0),
        ("heading", "1.2", ["1장"], 1),
        ("paragraph", "나", ["1장", "1.2"], 0),
        ("heading", "2장", [], 1),
        ("table", "다\t라", ["2장"], 0),
    ]
    assert [block.get("payload") for block in outline.result.blocks if block["block_type"] == "heading"] == [
        {"level": 1}, {"level": 2}, {"level": 2}, {"level": 1}
    ]


def test_a_block_with_its_own_ref_is_not_counted_under_the_headings():
    outline = Outline(Limits())
    outline.block("page", None, ref={"kind": "pdf_page", "page": 1})
    outline.block("paragraph", "첫 문단")
    outline.block("notes", "노트", ref={"kind": "slide", "slide": 2})
    outline.block("paragraph", "둘째 문단")

    assert [block["refs"] for block in outline.result.blocks] == [
        [{"kind": "pdf_page", "page": 1}],
        [{"kind": "doc_path", "heading_path": [], "block_index": 0}],
        [{"kind": "slide", "slide": 2}],
        [{"kind": "doc_path", "heading_path": [], "block_index": 1}],
    ]
    assert "text" not in outline.result.blocks[0]
    assert not outline.result.partial and outline.result.warnings == []


def test_a_long_block_is_cut_into_pieces_each_with_the_payload():
    outline = Outline(Limits(max_block_text_chars=3))
    outline.block("table_row", "가나다라마", payload={"formulas": {"B1": "A1"}}, ref={"kind": "document"})

    assert [(block["text"], block["payload"]) for block in outline.result.blocks] == [
        ("가나다", {"formulas": {"B1": "A1"}}), ("라마", {"formulas": {"B1": "A1"}})
    ]


def test_reading_stops_at_max_blocks_and_adds_nothing_after():
    outline = Outline(Limits(max_blocks=2))

    assert [outline.block("paragraph", "가"), outline.block("paragraph", "나"), outline.block("paragraph", "다"), outline.heading(1, "라")] == [
        True, True, False, False
    ]
    assert [block["text"] for block in outline.result.blocks] == ["가", "나"]
    assert outline.result.partial and outline.result.warnings == ["limit_reached"]


def test_reading_stops_inside_a_block_at_max_text_chars():
    outline = Outline(Limits(max_text_chars=4))

    assert outline.block("paragraph", "가나") and not outline.block("paragraph", "다라마")
    assert not outline.block("page", None, ref={"kind": "pdf_page", "page": 1})
    assert [block["text"] for block in outline.result.blocks] == ["가나", "다라"]
    assert outline.stopped and outline.result.partial and outline.result.warnings == ["limit_reached"]


def test_a_heading_cut_at_the_limit_opens_no_section():
    outline = Outline(Limits(max_text_chars=3))

    assert not outline.heading(1, "제목입니다")
    assert outline.result.blocks == [
        {"block_type": "heading", "text": "제목입", "refs": [{"kind": "doc_path", "heading_path": [], "block_index": 0}], "payload": {"level": 1}}
    ]
