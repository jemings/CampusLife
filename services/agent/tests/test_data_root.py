import json
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from campuslife_hub.data_root import Allowed, InsideSyncFolder, UncPath, check_data_root, detect_sync_roots, dropbox_roots, onedrive_roots


@pytest.mark.parametrize(
    ("root", "drive"),
    [
        (r"\\server\share\CampusLife", r"\\server\share"),
        ("//server/share/CampusLife", r"\\server\share"),
        (r"\\?\UNC\server\share\CampusLife", r"\\?\UNC\server\share"),
    ],
)
def test_a_unc_root_is_refused_and_names_its_drive(root, drive):
    assert check_data_root(PureWindowsPath(root), []) == UncPath(drive)


def test_a_unc_root_is_refused_before_the_sync_folders_are_looked_at():
    share = PureWindowsPath(r"\\server\share")
    assert check_data_root(share / "CampusLife", [share]) == UncPath(r"\\server\share")


def test_a_posix_path_starting_with_two_slashes_is_not_unc():
    root = PurePosixPath("//server/share/CampusLife")
    assert check_data_root(root, []) == Allowed(root)


def test_a_root_outside_every_sync_folder_is_allowed_as_it_is():
    root = PureWindowsPath(r"C:\Users\u\AppData\Local\CampusLife")
    assert check_data_root(root, [PureWindowsPath(r"C:\Users\u\OneDrive")]) == Allowed(root)


def test_a_root_under_a_sync_folder_is_refused_and_names_it():
    onedrive = PureWindowsPath(r"C:\Users\u\OneDrive")
    verdict = check_data_root(PureWindowsPath(r"C:\Users\u\OneDrive\Documents\CampusLife"), [onedrive])
    assert verdict == InsideSyncFolder(onedrive)


def test_the_sync_folder_itself_is_refused():
    dropbox = PurePosixPath("/home/u/Dropbox")
    assert check_data_root(dropbox, [dropbox]) == InsideSyncFolder(dropbox)


def test_windows_paths_compare_without_case():
    onedrive = PureWindowsPath(r"C:\Users\u\OneDrive")
    verdict = check_data_root(PureWindowsPath(r"c:\users\U\onedrive\CampusLife"), [onedrive])
    assert verdict == InsideSyncFolder(onedrive)


def test_a_sibling_whose_name_starts_like_the_sync_folder_is_allowed():
    root = PureWindowsPath(r"C:\Users\u\OneDriveBackup\CampusLife")
    assert check_data_root(root, [PureWindowsPath(r"C:\Users\u\OneDrive")]) == Allowed(root)


def test_every_onedrive_variable_names_a_sync_root():
    env = {
        "OneDrive": r"C:\Users\u\OneDrive",
        "OneDriveCommercial": r"C:\Users\u\OneDrive - School",
        "ONEDRIVECONSUMER": r"C:\Users\u\OneDrive",
        "PATH": r"C:\Windows",
        "OneDriveEmpty": "",
    }
    assert sorted(map(str, onedrive_roots(env))) == [r"C:\Users\u\OneDrive", r"C:\Users\u\OneDrive", r"C:\Users\u\OneDrive - School"]


def test_dropbox_info_lists_personal_and_business_roots():
    info = '{"personal": {"path": "C:\\\\Users\\\\u\\\\Dropbox"}, "business": {"path": "D:\\\\Dropbox (Team)", "host": 1}}'
    assert sorted(dropbox_roots(info)) == ["C:\\Users\\u\\Dropbox", "D:\\Dropbox (Team)"]


def test_an_unreadable_dropbox_info_names_no_root():
    assert dropbox_roots("not json") == []
    assert dropbox_roots('{"personal": "odd"}') == []


def test_detection_finds_dropbox_through_its_info_file_and_onedrive_through_the_environment(tmp_path, monkeypatch):
    dropbox, onedrive = tmp_path / "Dropbox", tmp_path / "OneDrive"
    dropbox.mkdir()
    onedrive.mkdir()
    info = json.dumps({"personal": {"path": str(dropbox)}})
    for info_dir in (tmp_path / "home" / ".dropbox", tmp_path / "appdata" / "Dropbox"):
        info_dir.mkdir(parents=True)
        (info_dir / "info.json").write_text(info, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    roots = detect_sync_roots({"APPDATA": str(tmp_path / "appdata"), "OneDrive": str(onedrive)})

    assert dropbox.resolve() in roots
    assert onedrive.resolve() in roots


def test_detection_without_sync_clients_finds_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert detect_sync_roots({"APPDATA": str(tmp_path)}) == []
