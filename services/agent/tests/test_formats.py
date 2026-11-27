import codecs

import pytest

import malicious_files as make
from campuslife_hub.extract import formats, sniff
from campuslife_hub.extract.formats import Reader, extension, refine
from campuslife_hub.extract.limits import Limits
from campuslife_hub.extract.protocol import Refused, Status

TEXT_READER = Reader("extract.text", {"text/plain": {"syntax": "plain"}, "text/markdown": {"syntax": "markdown"}})
CSV_READER = Reader("extract.table", {"text/csv": {}})
READERS = (TEXT_READER, CSV_READER)


@pytest.mark.parametrize(
    ("family", "filename", "declared", "detected", "jobs", "mismatch"),
    [
        ("text/plain", "notes.txt", None, "text/plain", [("extract.text", {"syntax": "plain"})], False),
        ("text/plain", "notes.md", None, "text/markdown", [("extract.text", {"syntax": "markdown"})], False),
        ("text/plain", "NOTES.MD", None, "text/markdown", [("extract.text", {"syntax": "markdown"})], False),
        ("text/plain", "notes.md", "text/plain", "text/markdown", [("extract.text", {"syntax": "markdown"})], False),
        ("text/plain", "notes.txt", "text/markdown", "text/markdown", [("extract.text", {"syntax": "markdown"})], False),
        ("text/plain", "grades.csv", None, "text/csv", [("extract.table", {})], False),
        ("text/plain", "main.py", None, "text/x-python", [], False),
        ("text/plain", None, None, "text/plain", [("extract.text", {"syntax": "plain"})], False),
        ("text/plain", "README", "application/octet-stream", "text/plain", [("extract.text", {"syntax": "plain"})], False),
        ("text/plain", "notes.unknownext", None, "text/plain", [("extract.text", {"syntax": "plain"})], False),
        ("application/pdf", "slides.pdf", "application/pdf", "application/pdf", [], False),
        ("application/pdf", "slides.docx", None, "application/pdf", [], True),
        ("application/pdf", "notes.md", None, "application/pdf", [], True),
        ("text/plain", "report.pdf", None, "text/plain", [("extract.text", {"syntax": "plain"})], True),
        ("text/plain", "notes.md", "application/pdf", "text/markdown", [("extract.text", {"syntax": "markdown"})], True),
        (formats.CFB, "report.hwp", None, "application/x-hwp", [], False),
        (formats.CFB, "report.doc", None, "application/msword", [], False),
        (formats.CFB, "report.docx", None, formats.CFB, [], True),
        (formats.DOCX, "report.docx", None, formats.DOCX, [], False),
        (formats.DOCX, "report.zip", None, formats.DOCX, [], True),
        (formats.UNKNOWN, "photo.heic", None, formats.UNKNOWN, [], True),
        (formats.UNKNOWN, "data.bin", None, formats.UNKNOWN, [], False),
    ],
)
def test_refine_narrows_the_bytes_family_by_the_declared_mime_then_the_name_and_the_bytes_win(family, filename, declared, detected, jobs, mismatch):
    refined = refine(family, filename, declared, READERS)

    assert (refined.detected_mime, [(job_type, dict(params)) for job_type, params in refined.jobs], refined.mismatch) == (detected, jobs, mismatch)


def test_refine_gives_a_job_for_every_reader_of_the_type():
    second = Reader("extract.other", {"text/markdown": {"mode": "x"}})

    refined = refine("text/plain", "a.md", None, (TEXT_READER, second))

    assert [(job_type, dict(params)) for job_type, params in refined.jobs] == [("extract.text", {"syntax": "markdown"}), ("extract.other", {"mode": "x"})]


@pytest.mark.parametrize(
    ("filename", "suffix"),
    [("a.md", ".md"), ("a.tar.GZ", ".gz"), ("dir/sub\\a.Txt", ".txt"), (".bashrc", None), ("noext", None), ("trailing.", None), (None, None), ("", None)],
)
def test_the_extension_is_the_last_suffix_of_the_last_path_part(filename, suffix):
    assert extension(filename) == suffix


def test_the_table_lists_every_format_of_file_ingestion_section_3_and_each_extension_once():
    listed = [suffix for each in formats.FORMATS for suffix in each.extensions]
    section_3 = ".pdf .pptx .ppt .docx .doc .hwpx .hwp .xlsx .csv .tsv .jpg .png .heic .webp .m4a .mp3 .wav .mp4 .mov .srt .vtt .txt .md .html .zip .py .c .cpp .java .js .ts .json .xml .yaml .ipynb"

    assert set(section_3.split()) <= set(listed)
    assert len(listed) == len(set(listed))


def write(path, data):
    path.write_bytes(data)
    return path


@pytest.mark.parametrize(
    ("make_file", "media_type"),
    [
        (make.pdf, formats.PDF),
        (make.plain_zip, formats.ZIP),
        (make.docx, formats.DOCX),
        (make.pptx, formats.PPTX),
        (make.xlsx, formats.XLSX),
        (make.hwpx, formats.HWPX),
        (make.ole_document, formats.CFB),
        (lambda path: write(path, "# 제목\n본문".encode()), formats.TEXT),
        (lambda path: write(path, codecs.BOM_UTF16_LE + "본문".encode("utf-16-le")), formats.TEXT),
        (lambda path: write(path, "한글 문서".encode("cp949")), formats.TEXT),
        (lambda path: write(path, b""), formats.TEXT),
        (lambda path: write(path, "가".encode() * 30000), formats.TEXT),
        (lambda path: write(path, b"\x89PNG\r\n\x1a\n" + bytes(16)), formats.PNG),
        (lambda path: write(path, b"\xff\xd8\xff\xe0" + bytes(16)), formats.JPEG),
        (lambda path: write(path, b"RIFF\0\0\0\0WEBPVP8 "), formats.WEBP),
        (lambda path: write(path, b"RIFF\0\0\0\0WAVEfmt "), formats.WAV),
        (lambda path: write(path, b"\0\0\0\x18ftypheic" + bytes(8)), formats.HEIC),
        (lambda path: write(path, b"\0\0\0\x18ftypM4A " + bytes(8)), formats.M4A),
        (lambda path: write(path, b"\0\0\0\x18ftypqt  " + bytes(8)), formats.MOV),
        (lambda path: write(path, b"\0\0\0\x18ftypisom" + bytes(8)), formats.MP4),
        (lambda path: write(path, b"ID3\x04" + bytes(16)), formats.MP3),
        (lambda path: write(path, b"\x00\x01binary\x00"), formats.UNKNOWN),
        (lambda path: write(path, "본문 without a BOM".encode("utf-16-le")), formats.UNKNOWN),
        (lambda path: write(path, b"\xc3\x28 not utf-8 nor cp949 \x80\xff"), formats.UNKNOWN),
    ],
)
def test_identify_reads_the_family_from_the_bytes(tmp_path, make_file, media_type):
    assert sniff.identify(make_file(tmp_path / "input"), Limits()) == media_type


@pytest.mark.parametrize(("make_file", "status"), [(make.encrypted_ooxml, Status.PASSWORD_REQUIRED), (make.traversal, Status.UNSAFE_INPUT)])
def test_identify_refuses_an_encrypted_office_file_and_a_dangerous_archive(tmp_path, make_file, status):
    with pytest.raises(Refused) as refused:
        sniff.identify(make_file(tmp_path / "input"), Limits())

    assert refused.value.status is status


def ole(path, size, marker_at):
    """An OLE container of size bytes with EncryptedPackage, in UTF-16, at marker_at (None for none)."""
    with path.open("wb") as handle:
        handle.write(sniff.CFB_MAGIC)
        handle.truncate(size)
        if marker_at is not None:
            handle.seek(marker_at)
            handle.write(sniff.ENCRYPTED_PACKAGE)
    return path


@pytest.mark.parametrize(
    "marker_at",
    [
        lambda: sniff.SCAN_BYTES - len(sniff.ENCRYPTED_PACKAGE) // 2,
        lambda: sniff.SCAN_BYTES - 1,
        lambda: sniff.SCAN_BYTES,
        lambda: 3 * sniff.SCAN_BYTES - len(sniff.ENCRYPTED_PACKAGE),
    ],
    ids=["across the first boundary", "one byte before it", "at it", "at the end"],
)
def test_an_encrypted_package_marker_anywhere_in_an_ole_file_is_found(tmp_path, marker_at):
    path = ole(tmp_path / "input", 3 * sniff.SCAN_BYTES, marker_at())

    with pytest.raises(Refused) as refused:
        sniff.identify(path, Limits())

    assert refused.value.status is Status.PASSWORD_REQUIRED


def test_an_ole_file_without_the_marker_is_cfb_whatever_its_chunks(tmp_path):
    assert sniff.identify(ole(tmp_path / "input", 3 * sniff.SCAN_BYTES + 7, None), Limits()) == formats.CFB


def test_identify_names_a_zip_bomb_an_archive_and_leaves_its_bytes_to_the_extractor(tmp_path):
    assert sniff.identify(make.bomb(tmp_path / "input"), Limits()) == formats.ZIP
