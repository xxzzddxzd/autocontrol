"""Worker stderr tails kept privately for diagnosis."""

from datetime import datetime, timedelta
import os
import stat

from autocontrol import storage
from autocontrol.schedule import BEIJING

STARTED = datetime(2026, 11, 18, 8, 0, 1, 956713, tzinfo=BEIJING)


def private_dir(tmp_path):
    path = tmp_path / 'private'
    path.mkdir(mode=0o700)
    return path


def test_a_stderr_tail_is_kept_privately_by_civil_day(tmp_path):
    private = private_dir(tmp_path)
    path = storage.keep_stderr(private, STARTED, 'auto', b'Error: boom\n')
    assert path == private / 'logs' / '2026-11-18' / '080001-956713-auto.stderr.log'
    assert path.read_bytes() == b'Error: boom\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for directory in (private / 'logs', path.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_nothing_is_written_for_an_empty_tail_or_a_shared_directory(tmp_path):
    private = private_dir(tmp_path)
    assert storage.keep_stderr(private, STARTED, 'auto', b'') is None
    assert not (private / 'logs').exists()
    shared = tmp_path / 'shared'
    shared.mkdir()
    os.chmod(shared, 0o755)
    assert storage.keep_stderr(shared, STARTED, 'auto', b'x') is None
    assert not (shared / 'logs').exists()


def test_a_label_cannot_leave_the_day_directory(tmp_path):
    private = private_dir(tmp_path)
    path = storage.keep_stderr(private, STARTED, '../../etc', b'x')
    assert path.parent == private / 'logs' / '2026-11-18'
    assert path.name == '080001-956713-______etc.stderr.log'


def test_only_the_last_seven_days_are_kept(tmp_path):
    private = private_dir(tmp_path)
    for days in (8, 7, 6):
        storage.keep_stderr(private, STARTED - timedelta(days=days), 'auto', b'x')
    (private / 'logs' / 'notes').mkdir(mode=0o700)
    storage.keep_stderr(private, STARTED, 'auto', b'x')
    assert sorted(path.name for path in (private / 'logs').iterdir()) == [
        '2026-11-12',
        '2026-11-18',
        'notes',
    ]
