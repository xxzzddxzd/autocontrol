import fcntl

import pytest

from autocontrol import locks


def test_probing_another_programs_lock_ignores_its_permission_bits(tmp_path):
    folder = tmp_path / 'reports'
    folder.mkdir(mode=0o755)
    lock = folder / 'main-account.lock'
    assert locks.is_free(lock) and not lock.exists()  # a probe never creates the file
    lock.touch()
    lock.chmod(0o644)
    assert locks.is_free(lock)
    with open(lock) as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert not locks.is_free(lock)
    assert locks.is_free(lock)
    assert lock.stat().st_mode & 0o777 == 0o644  # left as the other program made it
    assert locks.is_free(tmp_path / 'missing' / 'main-account.lock')
    with pytest.raises(ValueError):
        locks.is_free(folder)


def test_the_consoles_own_locks_stay_strict(tmp_path):
    folder = tmp_path / 'private'
    folder.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        locks.acquire(folder / 'round.lock')
