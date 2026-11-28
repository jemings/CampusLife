"""The CFB writer tests use is checked against an independent reader, olefile, so the files it makes are ones a real
parser takes."""

import olefile
import pytest

import cfb_writer

NAMES = ["WordDocument", "1Table", "\x05SummaryInformation", "Data", "ObjectPool", "\x01CompObj", "Workbook"]


@pytest.mark.parametrize("count", range(1, len(NAMES) + 1))
def test_olefile_reads_back_every_stream_of_a_flat_file(tmp_path, count):
    streams = {name: f"{name} 내용".encode("utf-16-le") * (index + 1) for index, name in enumerate(NAMES[:count])}
    path = cfb_writer.write(tmp_path / "flat.cfb", streams)

    with olefile.OleFileIO(str(path), raise_defects=olefile.DEFECT_INCORRECT) as ole:
        assert sorted(entry[0] for entry in ole.listdir()) == sorted(streams)
        for name, data in streams.items():
            assert ole.openstream(name).read()[: len(data)] == data


def test_olefile_reads_storages_and_a_stream_larger_than_a_fat_sector_covers(tmp_path):
    large = bytes(range(256)) * 400
    path = cfb_writer.write(tmp_path / "nested.cfb", {"\x06DataSpaces": {"Version": b"v" * 10}, "EncryptedPackage": large})

    with olefile.OleFileIO(str(path), raise_defects=olefile.DEFECT_INCORRECT) as ole:
        assert sorted(ole.listdir(streams=True, storages=True)) == [["\x06DataSpaces"], ["\x06DataSpaces", "Version"], ["EncryptedPackage"]]
        assert ole.openstream("EncryptedPackage").read() == large
        assert ole.get_type("\x06DataSpaces") == olefile.STGTY_STORAGE


def test_each_storage_holds_a_red_black_tree_in_the_specs_order(tmp_path):
    path = cfb_writer.write(tmp_path / "tree.cfb", dict.fromkeys(NAMES, b"x"))

    with olefile.OleFileIO(str(path)) as ole:
        root = ole.direntries[0]
        nodes = {index: ole.direntries[index] for index in range(1, len(NAMES) + 1)}

        def walk(index, blacks):
            """The in-order names and the black count of every path to a leaf."""
            if index == olefile.NOSTREAM:
                return [], {blacks}
            node = nodes[index]
            blacks += node.color == 1
            if node.color == 0:
                assert all(nodes[child].color == 1 for child in (node.sid_left, node.sid_right) if child != olefile.NOSTREAM)
            left, left_blacks = walk(node.sid_left, blacks)
            right, right_blacks = walk(node.sid_right, blacks)
            return [*left, node.name, *right], left_blacks | right_blacks

        names, black_heights = walk(root.sid_child, 0)
    assert names == sorted(NAMES, key=lambda name: (len(name), name.upper()))
    assert len(black_heights) == 1


def test_trailing_bytes_no_sector_holds_leave_the_file_readable(tmp_path):
    path = cfb_writer.write(tmp_path / "padded.cfb", {"WordDocument": b"w"}, trailing=1 << 20)

    with olefile.OleFileIO(str(path)) as ole:
        assert ole.listdir() == [["WordDocument"]]
