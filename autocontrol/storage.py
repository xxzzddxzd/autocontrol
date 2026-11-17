"""Private result archive: one atomic, fixed-field JSON file per phase run.

Files live in ``<private>/results/<civil date>/``. They are an audit trail for
people, never an input to execution: the worker always re-reads the server.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import stat


def open_private_directory(path, *, parent=None, create=False) -> int:
    """Open (optionally create) a 0700 directory owned by us, without following links."""
    if create:
        try:
            os.mkdir(path, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(descriptor)
        raise PermissionError('result directory must be private')
    return descriptor


def save_record(day_fd: int, started: datetime, command: str, record: dict) -> str:
    """Write ``record`` under a unique name; the final name appears atomically."""
    name = f'{started:%H%M%S-%f}-{command}-{secrets.token_hex(12)}.json'
    temporary = '.' + name + '.tmp'
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=day_fd
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(record, output, ensure_ascii=True, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fchmod(output.fileno(), 0o600)
            os.fsync(output.fileno())
        os.link(temporary, name, src_dir_fd=day_fd, dst_dir_fd=day_fd, follow_symlinks=False)
    finally:
        os.unlink(temporary, dir_fd=day_fd)
    return name


def archive(private, started: datetime, command: str, record: dict) -> Path:
    """Save one record below ``private/results/<civil date>``; raises ``OSError``."""
    private = Path(private)
    civil_day = started.date().isoformat()
    private_fd = open_private_directory(private)
    try:
        results_fd = open_private_directory('results', parent=private_fd, create=True)
        try:
            day_fd = open_private_directory(civil_day, parent=results_fd, create=True)
            try:
                name = save_record(day_fd, started, command, record)
            finally:
                os.close(day_fd)
        finally:
            os.close(results_fd)
    finally:
        os.close(private_fd)
    return private / 'results' / civil_day / name
