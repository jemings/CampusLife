import codecs
import io
import zipfile

import pytest

import cfb_writer
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
        ("application/x-hwp", "report.hwp", None, "application/x-hwp", [], False),
        ("application/msword", "report.doc", None, "application/msword", [], False),
        ("application/msword", "report.docx", None, "application/msword", [], True),
        ("application/msword", "grades.xls", None, "application/msword", [], True),
        (formats.CFB, "report.doc", None, formats.CFB, [], True),
        (formats.CFB, "mail.msg", None, formats.CFB, [], False),
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
        (make.ole_document, "application/msword"),
        (lambda path: cfb_writer.write(path, {"Workbook": b"w"}), "application/vnd.ms-excel"),
        (lambda path: cfb_writer.write(path, {"Book": b"b"}), "application/vnd.ms-excel"),
        (lambda path: cfb_writer.write(path, {"PowerPoint Document": b"p", "Current User": b"u"}), "application/vnd.ms-powerpoint"),
        (lambda path: cfb_writer.write(path, {"FileHeader": b"HWP Document File", "BodyText": {"Section0": b"s"}}), "application/x-hwp"),
        (lambda path: cfb_writer.write(path, {"__substg1.0_0037001F": b"m"}), formats.CFB),
        (lambda path: write(path, b"\x00" * 1019 + b"%PDF-1.7\n"), formats.PDF),
        (lambda path: write(path, b"junk before the header\r\n%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + bytes(range(256))), formats.PDF),
        (lambda path: write(path, b"junk before the header\r\n%PDF-1.4\n"), formats.TEXT),
        (lambda path: cfb_writer.write(path, {"WordDocument": b"w", "Workbook": b"b"}), "application/msword"),
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


@pytest.mark.parametrize(
    ("make_file", "status"),
    [
        (make.encrypted_ooxml, Status.PASSWORD_REQUIRED),
        (lambda path: cfb_writer.write(path, {"\x06DataSpaces": {"Version": b"v"}}), Status.PASSWORD_REQUIRED),
        (lambda path: cfb_writer.write(path, {"WordDocument": b"w", "\x06DataSpaces": {"Version": b"v"}}), Status.PASSWORD_REQUIRED),
        (make.traversal, Status.UNSAFE_INPUT),
        (lambda path: write(path, cfb_writer.MAGIC + bytes(600)), Status.CORRUPT),
    ],
    ids=["encrypted_package", "data_spaces", "encrypted_beside_a_word_stream", "traversal", "damaged_cfb"],
)
def test_identify_refuses_an_encrypted_office_file_a_dangerous_archive_and_a_damaged_container(tmp_path, make_file, status):
    with pytest.raises(Refused) as refused:
        sniff.identify(make_file(tmp_path / "input"), Limits())

    assert refused.value.status is status


def test_a_word_document_whose_text_says_encrypted_package_is_a_word_document(tmp_path):
    """The #5 PR #52 regression: the word in a stream's bytes is not a root entry."""
    text = "이 문서는 EncryptedPackage 스트림을 설명합니다".encode("utf-16-le")
    path = cfb_writer.write(tmp_path / "input", {"WordDocument": text * 300, "1Table": text})

    assert sniff.identify(path, Limits()) == "application/msword"


@pytest.mark.parametrize(
    "data",
    ["# PDF 파일 구조\n\nPDF 파일은 %PDF-1.7 으로 시작합니다.\n".encode(), b"def is_pdf(data):\n    return data.startswith(b'%PDF-')\n"],
    ids=["markdown_note", "python_code"],
)
def test_text_that_mentions_the_pdf_header_is_text(tmp_path, data):
    """A PDF header after other bytes counts only when those bytes are no text: a note or code about PDF is text."""
    assert sniff.identify(write(tmp_path / "input", data), Limits()) == formats.TEXT


@pytest.mark.parametrize("offset", [1020, 4000])
def test_a_pdf_header_past_the_first_1024_bytes_is_not_a_pdf(tmp_path, offset):
    path = write(tmp_path / "input", b" " * offset + b"%PDF-1.7\n")

    assert sniff.identify(path, Limits()) == formats.TEXT


def test_a_signature_at_the_start_comes_before_a_pdf_header_inside_the_head(tmp_path):
    """A ZIP that stores a PDF uncompressed shows %PDF- in its first bytes; it is still a ZIP."""
    path = tmp_path / "input"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("a.pdf", b"%PDF-1.7\n")

    assert sniff.identify(path, Limits()) == formats.ZIP


def test_identify_names_a_zip_bomb_an_archive_and_leaves_its_bytes_to_the_extractor(tmp_path):
    assert sniff.identify(make.bomb(tmp_path / "input"), Limits()) == formats.ZIP


def hwpx_with_mimetype(path, mimetype):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("mimetype", mimetype)
        archive.writestr("Contents/section0.xml", b"<x/>")
    path.write_bytes(buffer.getvalue())
    return path


@pytest.mark.parametrize(
    ("size", "media_type"), [(sniff.MIMETYPE_MAX_BYTES, formats.HWPX), (sniff.MIMETYPE_MAX_BYTES + 1, formats.ZIP)], ids=["at the bound", "past it"],
)
def test_an_hwpx_mimetype_is_read_up_to_its_bound(tmp_path, size, media_type):
    mimetype = formats.HWPX.encode().ljust(size, b"\n")

    assert sniff.identify(hwpx_with_mimetype(tmp_path / "a.hwpx", mimetype), Limits()) == media_type
