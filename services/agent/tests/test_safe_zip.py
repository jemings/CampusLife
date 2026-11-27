import pytest

import malicious_files as make
from campuslife_hub.extract.limits import Limits
from campuslife_hub.extract.protocol import Refused, Status
from campuslife_hub.extract.safe_zip import open_zip

LIMITS = Limits()


def refusal(path, limits=LIMITS):
    with pytest.raises(Refused) as refused, open_zip(path, limits):
        pass
    return refused.value.status


def test_a_plain_archive_opens_and_its_members_read_back(tmp_path):
    with open_zip(make.plain_zip(tmp_path / "a.zip"), LIMITS) as archive:
        assert [(member.name, member.size) for member in archive.members] == [("notes/week1.txt", len("1주차 노트".encode())), ("notes/week2.txt", 6)]
        assert archive.read("notes/week1.txt").decode() == "1주차 노트"


@pytest.mark.parametrize(
    ("case", "status"),
    [
        (make.traversal, Status.UNSAFE_INPUT),
        (make.absolute, Status.UNSAFE_INPUT),
        (make.drive_letter, Status.UNSAFE_INPUT),
        (make.symlink, Status.UNSAFE_INPUT),
        (make.bomb, Status.UNSAFE_INPUT),
        (make.listed_twice, Status.UNSAFE_INPUT),
        (make.encrypted_member, Status.PASSWORD_REQUIRED),
    ],
)
def test_a_dangerous_archive_is_refused_before_any_member_is_read(tmp_path, case, status):
    assert refusal(case(tmp_path / "x.zip")) is status


def test_bytes_that_are_no_zip_are_corrupt(tmp_path):
    path = tmp_path / "x.zip"
    path.write_bytes(b"PK\x03\x04 not really")

    assert refusal(path) is Status.CORRUPT


@pytest.mark.parametrize(
    ("limits", "case"),
    [
        (Limits(zip_max_members=3), lambda path: make.many_members(path, 4)),
        (Limits(zip_max_member_bytes=5), make.plain_zip),
        (Limits(zip_max_total_bytes=len("1주차 노트".encode()) + 5), make.plain_zip),
        (Limits(zip_max_ratio=2), make.compressible),
    ],
    ids=["members", "member size", "total size", "ratio"],
)
def test_each_zip_limit_of_the_table_holds(tmp_path, limits, case):
    path = case(tmp_path / "x.zip")

    assert refusal(path, limits) is Status.UNSAFE_INPUT
    with open_zip(path, Limits()) as archive:
        assert archive.members


def test_a_member_that_declares_fewer_bytes_than_it_holds_is_read_to_its_declared_size_and_found_corrupt(tmp_path):
    with open_zip(make.understated(tmp_path / "x.zip"), LIMITS) as archive, pytest.raises(Refused) as refused:
        archive.read("a.txt")

    assert refused.value.status is Status.CORRUPT


def test_a_member_name_ends_at_its_first_nul(tmp_path):
    with open_zip(make.nul_in_name(tmp_path / "x.zip"), LIMITS) as archive:
        assert [member.name for member in archive.members] == ["notes.txt"]
        assert archive.read("notes.txt") == b"notes"


def test_the_limits_allow_exactly_their_bound(tmp_path):
    path = make.many_members(tmp_path / "x.zip", 3)
    with open_zip(path, Limits(zip_max_members=3)) as archive:
        assert len(archive.members) == 3
    total = len("1주차 노트".encode()) + 6
    with open_zip(make.plain_zip(tmp_path / "p.zip"), Limits(zip_max_total_bytes=total, zip_max_member_bytes=total - 6)) as archive:
        assert len(archive.members) == 2


def test_nested_archives_open_up_to_the_depth_of_the_table(tmp_path):
    path = make.nested(tmp_path / "n.zip", 3)
    with open_zip(path, Limits(zip_max_depth=3)) as outer, outer.open_nested("inner.zip") as middle, middle.open_nested("inner.zip") as inner:
        assert inner.read("inner.txt") == b"innermost"
    with open_zip(path, Limits(zip_max_depth=2)) as outer, outer.open_nested("inner.zip") as middle:
        with pytest.raises(Refused) as refused, middle.open_nested("inner.zip"):
            pass
    assert refused.value.status is Status.UNSAFE_INPUT
