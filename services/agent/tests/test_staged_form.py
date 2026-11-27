import pytest

from campuslife_hub import staged_form
from campuslife_hub.staged_form import MAX_FIELD_BYTES, FormRefused, stage_form

FORM = "multipart/form-data; boundary=x"


def part(name, content, filename=None):
    disposition = f'form-data; name="{name}"' + ("" if filename is None else f'; filename="{filename}"')
    return f"--x\r\nContent-Disposition: {disposition}\r\n\r\n".encode() + content + b"\r\n"


def body(*parts):
    return b"".join(parts) + b"--x--\r\n"


async def streamed(data, *, size=7, then=None):
    for start in range(0, len(data), size):
        yield data[start : start + size]
    if then is not None:
        raise then


async def test_files_stream_into_staging_files_of_their_own_and_fields_are_kept(tmp_path):
    data = body(part("files", b"first file " * 50, "week 4.txt"), part("cloud_policy", b"blocked"), part("files", b"second", "자료.pdf"))

    form = await stage_form(FORM, streamed(data), tmp_path)

    assert form.fields == {"cloud_policy": "blocked"}
    assert [(staged.name, staged.path.read_bytes()) for staged in form.files] == [("week 4.txt", b"first file " * 50), ("자료.pdf", b"second")]
    assert all(staged.path.parent == tmp_path and staged.path.suffix == ".part" for staged in form.files)
    form.discard()
    assert list(tmp_path.iterdir()) == []


async def test_a_file_input_left_empty_stages_nothing(tmp_path):
    form = await stage_form(FORM, streamed(body(part("files", b"", ""))), tmp_path)

    assert (form.files, list(tmp_path.iterdir())) == ([], [])


async def test_a_body_that_breaks_off_leaves_no_staging_file(tmp_path):
    data = body(part("files", b"x" * 100, "one.txt"), part("files", b"y" * 100, "two.txt"))

    with pytest.raises(ConnectionResetError):
        await stage_form(FORM, streamed(data[:-60], then=ConnectionResetError()), tmp_path)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "data",
    [
        body(part("files", b"x", "one.txt"), part("note", b"n" * (MAX_FIELD_BYTES + 1))),
        body(part("files", b"x", "one.txt"), *(part(f"f{n}", b"v") for n in range(17))),
        body(part("files", b"x", "one.txt"), part("files", b"y", "two.txt"), part("files", b"z", "three.txt")),
        b"--x\r\nContent-Disposition: form-data; name=\"files\"; filename=\"one.txt\"\r\n" + b"X-Long: " + b"h" * 8000 + b"\r\n\r\nx\r\n--x--\r\n",
    ],
    ids=["field too long", "too many fields", "too many files", "header too long"],
)
async def test_a_form_past_its_bounds_is_refused_and_leaves_no_staging_file(tmp_path, monkeypatch, data):
    monkeypatch.setattr(staged_form, "MAX_FILES", 2)

    with pytest.raises(FormRefused):
        await stage_form(FORM, streamed(data, size=1000), tmp_path)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("content_type", ["text/plain", "multipart/form-data", "application/x-www-form-urlencoded", ""])
async def test_a_body_that_is_not_a_multipart_form_is_refused(tmp_path, content_type):
    with pytest.raises(FormRefused):
        await stage_form(content_type, streamed(b"files=x"), tmp_path)


@pytest.mark.parametrize("content_type", ["text/plain; boundary=x", "multipart/mixed; boundary=x"])
async def test_a_well_formed_multipart_body_under_another_type_is_refused(tmp_path, content_type):
    with pytest.raises(FormRefused):
        await stage_form(content_type, streamed(body(part("files", b"x", "one.txt"))), tmp_path)

    assert list(tmp_path.iterdir()) == []
