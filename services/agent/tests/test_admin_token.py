import stat
import sys

import pytest

from campuslife_hub import admin_token
from campuslife_hub.admin_token import AdminTokenUnusable

WINDOWS = sys.platform == "win32"


def test_a_token_is_made_once_and_read_back_the_same(tmp_path):
    path = tmp_path / "admin.token"

    made = admin_token.load_or_create(path)

    assert len(made) >= 43
    assert admin_token.load_or_create(path) == made
    assert admin_token.read(path) == made
    with pytest.raises(FileExistsError):
        admin_token.create(path)


def test_two_data_roots_get_different_tokens(tmp_path):
    assert admin_token.load_or_create(tmp_path / "a.token") != admin_token.load_or_create(tmp_path / "b.token")


def test_a_missing_token_file_is_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        admin_token.read(tmp_path / "admin.token")


@pytest.mark.skipif(WINDOWS, reason="on Windows DPAPI binds the token to the user")
def test_only_the_owner_may_read_the_token_file_and_a_shared_one_is_refused(tmp_path):
    path = tmp_path / "admin.token"
    admin_token.create(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    path.chmod(0o640)
    with pytest.raises(AdminTokenUnusable, match="may be read by others"):
        admin_token.read(path)


@pytest.mark.skipif(not WINDOWS, reason="DPAPI is Windows only")
def test_on_windows_the_file_holds_the_token_sealed_with_dpapi(tmp_path):
    path = tmp_path / "admin.token"

    token = admin_token.create(path)

    sealed = path.read_bytes()
    assert token.encode("ascii") not in sealed
    assert admin_token.unseal(sealed) == token.encode("ascii")
    assert admin_token.unseal(admin_token.seal(b"round trip")) == b"round trip"


@pytest.mark.skipif(not WINDOWS, reason="DPAPI is Windows only")
def test_on_windows_a_file_dpapi_cannot_unseal_is_refused(tmp_path):
    path = tmp_path / "admin.token"
    path.write_bytes(b"not a DPAPI blob")

    with pytest.raises(AdminTokenUnusable, match="cannot be unsealed"):
        admin_token.read(path)


@pytest.mark.skipif(WINDOWS, reason="on Windows the bytes are a DPAPI blob")
def test_a_file_that_holds_no_token_is_refused(tmp_path):
    path = tmp_path / "admin.token"
    path.write_bytes("토큰".encode())
    path.chmod(0o600)

    with pytest.raises(AdminTokenUnusable, match="does not hold an admin token"):
        admin_token.read(path)


@pytest.mark.parametrize("held", [b"", b"short", b"x" * 43 + b"!", b"y" * 44], ids=["empty", "short", "not url-safe", "too long"])
def test_a_file_whose_token_is_not_one_the_hub_makes_is_refused(tmp_path, held):
    path = tmp_path / "admin.token"
    path.write_bytes(admin_token.seal(held) if held else b"")
    path.chmod(0o600)

    with pytest.raises(AdminTokenUnusable):
        admin_token.read(path)


def test_a_token_that_cannot_be_sealed_leaves_no_file(tmp_path, monkeypatch):
    def failing(data):
        raise OSError("the sealing service is not running")

    monkeypatch.setattr(admin_token, "seal", failing)

    with pytest.raises(OSError):
        admin_token.load_or_create(tmp_path / "admin.token")

    assert list(tmp_path.iterdir()) == []


def test_a_file_a_crash_left_half_written_beside_the_token_is_replaced(tmp_path):
    path = tmp_path / "admin.token"
    (tmp_path / "admin.token.new").write_bytes(b"half")

    made = admin_token.load_or_create(path)

    assert admin_token.read(path) == made
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["admin.token"]
