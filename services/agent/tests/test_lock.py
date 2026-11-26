import subprocess
import sys

import pytest

from campuslife_hub.lock import AlreadyRunning, HubLock


def test_a_second_holder_is_refused_while_the_first_holds(tmp_path):
    lock_file = tmp_path / "hub.lock"
    with HubLock.acquire(lock_file):
        with pytest.raises(AlreadyRunning):
            HubLock.acquire(lock_file)


def test_the_lock_can_be_taken_again_after_release(tmp_path):
    lock_file = tmp_path / "hub.lock"
    with HubLock.acquire(lock_file):
        pass
    with HubLock.acquire(lock_file):
        pass


def test_a_lock_file_left_by_a_dead_process_does_not_hold_the_lock(tmp_path):
    lock_file = tmp_path / "hub.lock"
    script = (
        "import os, sys\n"
        "from campuslife_hub.lock import HubLock\n"
        "HubLock.acquire(sys.argv[1])\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", script, str(lock_file)], check=True)
    assert lock_file.exists()
    with HubLock.acquire(lock_file):
        pass


def test_another_process_is_refused_while_this_one_holds(tmp_path):
    lock_file = tmp_path / "hub.lock"
    script = (
        "import sys\n"
        "from campuslife_hub.lock import AlreadyRunning, HubLock\n"
        "try:\n"
        "    HubLock.acquire(sys.argv[1])\n"
        "except AlreadyRunning:\n"
        "    sys.exit(3)\n"
    )
    with HubLock.acquire(lock_file):
        finished = subprocess.run([sys.executable, "-c", script, str(lock_file)])
    assert finished.returncode == 3


def test_the_lock_names_its_file(tmp_path):
    with HubLock.acquire(tmp_path / "hub.lock") as held:
        assert held.lock_file == (tmp_path / "hub.lock").resolve()


def test_a_lock_holds_only_its_own_file_and_only_until_released(tmp_path):
    (tmp_path / "other").mkdir()
    with HubLock.acquire(tmp_path / "hub.lock") as held:
        assert held.holds(tmp_path / "hub.lock")
        assert not held.holds(tmp_path / "other" / "hub.lock")
        held.release()
        assert not held.holds(tmp_path / "hub.lock")
    with HubLock.acquire(tmp_path / "hub.lock"):
        pass
