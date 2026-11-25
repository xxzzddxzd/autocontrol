"""Private result archive: one atomic, fixed-field JSON file per phase run.

Files live in ``<private>/results/<civil date>/``. They are an audit trail for
people, never an input to execution: the worker always re-reads the server.
The tail of a worker's stderr may be kept beside them in
``<private>/logs/<civil date>/`` for a few days, for people only as well.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import secrets
import stat

LOG_DAYS = 7


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


def keep_stderr(private, started: datetime, label: str, data: bytes, *, days: int = LOG_DAYS):
    """Best effort: save a worker's stderr tail below ``private/logs/<civil date>``.

    The file (0600) is for people diagnosing a failure; it is never published
    or read by the console. Log days older than ``days`` are removed. Returns
    the path, or ``None`` when there was nothing to keep or it was not written.
    """
    if not data:
        return None
    private = Path(private)
    civil_day = started.date().isoformat()
    label = re.sub(r'[^a-z0-9_-]', '_', str(label).lower())[:40] or 'worker'
    name = f'{started:%H%M%S-%f}-{label}.stderr.log'
    try:
        private_fd = open_private_directory(private)
        try:
            logs_fd = open_private_directory('logs', parent=private_fd, create=True)
            try:
                _prune_logs(logs_fd, started.date(), days)
                day_fd = open_private_directory(civil_day, parent=logs_fd, create=True)
                try:
                    fd = os.open(
                        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                        dir_fd=day_fd,
                    )  # fmt: skip
                    with os.fdopen(fd, 'wb') as output:
                        output.write(data)
                finally:
                    os.close(day_fd)
            finally:
                os.close(logs_fd)
        finally:
            os.close(private_fd)
    except (OSError, ValueError):
        return None
    return private / 'logs' / civil_day / name


def _prune_logs(logs_fd: int, today: date, days: int) -> None:
    oldest = today - timedelta(days=max(1, days) - 1)
    for name in os.listdir(logs_fd):
        try:
            day = date.fromisoformat(name)
        except ValueError:
            continue
        if day.isoformat() != name or day >= oldest:
            continue
        try:
            day_fd = open_private_directory(name, parent=logs_fd)
        except OSError:
            continue
        try:
            for entry in os.listdir(day_fd):
                try:
                    os.unlink(entry, dir_fd=day_fd)
                except OSError:
                    pass
        finally:
            os.close(day_fd)
        try:
            os.rmdir(name, dir_fd=logs_fd)
        except OSError:
            pass
