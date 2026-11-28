import struct

import pytest

import cfb_writer
from campuslife_hub.extract import cfb
from campuslife_hub.extract.cfb import open_cfb
from campuslife_hub.extract.protocol import Refused, Status


def refusal(path, action=lambda compound: compound.root_entries()):
    with pytest.raises(Refused) as refused, open_cfb(path) as compound:
        action(compound)
    return refused.value.status


def test_the_root_entries_and_a_stream_read_back(tmp_path):
    path = cfb_writer.write(tmp_path / "a.doc", {"WordDocument": "본문".encode("utf-16-le"), "\x06DataSpaces": {"Version": b"1"}})

    with open_cfb(path) as compound:
        assert compound.root_entries() == {"WordDocument", "\x06DataSpaces"}
        assert compound.read(["WordDocument"], max_bytes=4096).startswith("본문".encode("utf-16-le"))
        assert compound.read(["\x06DataSpaces", "Version"], max_bytes=4096)[:1] == b"1"


def test_a_stream_larger_than_its_bound_is_unsafe_and_not_read(tmp_path):
    path = cfb_writer.write(tmp_path / "a.hwp", {"FileHeader": b"h" * 5000})

    assert refusal(path, lambda compound: compound.read(["FileHeader"], max_bytes=4999)) is Status.UNSAFE_INPUT


def test_a_missing_stream_is_corrupt(tmp_path):
    path = cfb_writer.write(tmp_path / "a.hwp", {"FileHeader": b"h"})

    assert refusal(path, lambda compound: compound.read(["BodyText", "Section0"], max_bytes=4096)) is Status.CORRUPT


def patched(path, offset, value):
    data = bytearray(path.read_bytes())
    data[offset:offset + len(value)] = value
    path.write_bytes(bytes(data))
    return path


@pytest.mark.parametrize(
    "damage",
    [
        lambda path: path.write_bytes(b"not a compound file at all" * 40) or path,
        lambda path: path.write_bytes(path.read_bytes()[:700]) or path,
        lambda path: patched(path, 0x30, struct.pack("<I", 0x7FFFFF00)),
        lambda path: patched(path, 0x1E, struct.pack("<H", 30)),
    ],
    ids=["not_cfb", "truncated", "directory_past_the_end", "sector_shift_absurd"],
)
def test_a_damaged_compound_file_is_corrupt(tmp_path, damage):
    path = damage(cfb_writer.write(tmp_path / "a.doc", {"WordDocument": b"w" * 9000}))

    assert refusal(path, lambda compound: (compound.root_entries(), compound.read(["WordDocument"], max_bytes=1 << 20))) is Status.CORRUPT


def test_running_out_of_memory_in_olefile_is_not_taken_for_a_damaged_file(tmp_path, monkeypatch):
    def exhausted(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(cfb.olefile, "OleFileIO", exhausted)

    with pytest.raises(MemoryError), open_cfb(cfb_writer.write(tmp_path / "a.doc", {"WordDocument": b"w"})):
        pass


def test_a_fat_chain_that_loops_ends_with_the_streams_declared_size(tmp_path):
    """olefile does not refuse a loop: it reads sectors until the size the directory declares, so a loop costs
    nothing past that size, and what it reads is the looped sector again. A format reader judges that content."""
    path = cfb_writer.write(tmp_path / "a.doc", {"WordDocument": bytes(range(256)) * 36})
    first_data_sector = 2
    patched(path, 512 + 4 * first_data_sector, struct.pack("<I", first_data_sector))

    with open_cfb(path) as compound:
        data = compound.read(["WordDocument"], max_bytes=1 << 20)

    assert len(data) == 256 * 36 and data[512:1024] == data[:512]
