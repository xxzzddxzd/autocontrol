"""Advisory ``flock`` locks.

The console's own locks live in its private directory and are checked
strictly. Another program's account lock is only probed (``is_free``): the
console never creates, changes or holds it, so the probe needs no login and
cannot change game state.
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
    """Probe a lock another program may hold, without creating or changing it.

    Only whether someone holds the flock matters: the file belongs to that
    program, so its permission bits are its own business. A missing file or
    directory means free; anything but a regular file is an error.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('account lock is not a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    finally:
        os.close(fd)
