"""Advisory ``flock`` locks inside a private directory.

The file checks match the game worker's own run lock, so the console can probe
the worker's account lock without logging in or changing game state.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat


class AlreadyRunning(RuntimeError):
    """Another process currently holds the lock."""


def check_private_dir(path) -> None:
    info = Path(path).lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError('runtime directory must be private (0700)')


def acquire(path) -> int:
    """Take the lock without blocking; return the descriptor that holds it."""
    path = Path(path)
    check_private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError('invalid runtime lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(path.name) from None
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def held(path):
    fd = acquire(path)
    try:
        yield fd
    finally:
        os.close(fd)


def is_free(path) -> bool:
    """Probe a lock someone else may hold; a missing directory means free."""
    try:
        fd = acquire(path)
    except AlreadyRunning:
        return False
    except FileNotFoundError:
        return True
    os.close(fd)
    return True
