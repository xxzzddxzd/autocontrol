"""Run one yslzm worker phase and archive its fixed-field result.

Ported from ``yslzm/tools/run_scheduled.py``. The worker is ``main.py <phase>``
inside the yslzm checkout. Its stdout stays in memory; only the summarized
record is saved under ``<private>/results/<civil date>/``. A phase is never
replayed because its result could not be stored.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import stat
import subprocess
import sys

from ... import storage
from ...worker import run_streamed
from .summary import (  # noqa: F401  (re-exported for callers and tests)
    BEIJING,
    ERROR_TYPES,
    MAX_SCOPE_BYTES,
    MAX_SUMMARY_BYTES,
    allowed,
    iso_day,
    latest_summary,
    needs_fresh_session,
    result_exit_code,
    summarize,
)

COMMANDS = ('auto', 'daily', 'cardbook', 'craft', 'activities', 'wardrobe')
PROGRESS_ENV = 'YSLZM_PROGRESS_FD'
DEFAULT_PROJECT = Path.home() / 'Documents/workspace/yslzm'
DEFAULT_PRIVATE = Path.home() / '.config/yslzm'


def launch_failure(stage, error) -> dict:
    return {
        'exit_code': None,
        'execution': 'failed',
        'failure_stage': stage,
        'error_type': allowed(type(error).__name__, ERROR_TYPES, 'OtherError'),
    }


def _scope_rows(value):
    """Validate the in-memory set of requests sent this round without a result."""
    if type(value) is not dict or set(value) - {'uncertain_targets'}:
        return None
    rows = value.get('uncertain_targets', [])
    if type(rows) is not list or len(rows) > 2048:
        return None
    for row in rows:
        if type(row) not in (list, tuple) or not 1 <= len(row) <= 8:
            return None
        if any(
            type(item) not in (str, int) or type(item) is str and len(item) > 4096 for item in row
        ):
            return None
    return [tuple(row) for row in rows]


def merge_run_scope(run_scope, summary) -> None:
    if run_scope is None or type(summary) is not dict or 'run_scope' not in summary:
        return
    previous, current = _scope_rows(run_scope), _scope_rows(summary['run_scope'])
    if previous is None or current is None:
        return
    merged = sorted(set(previous) | set(current), key=repr)
    if len(merged) <= 2048:
        run_scope['uncertain_targets'] = [list(row) for row in merged]


def command_line(command, target_day, *, python=None) -> list[str]:
    return [
        python or sys.executable, '-B', '-u', 'main.py', command,
        '--not-before-game-day', target_day, '--scheduled-result',
    ]  # fmt: skip


def run_child(
    project, command, target_day, timeout, *, run_scope=None, on_event=None, python=None,
    on_stderr=None,
):  # fmt: skip
    try:
        if not stat.S_ISDIR(Path(project).stat().st_mode):
            raise NotADirectoryError()
    except OSError as error:
        return launch_failure('project_cwd', error)
    argv = command_line(command, target_day, python=python)
    options = {}
    if run_scope is not None:
        rows = _scope_rows(run_scope)
        if rows is None:
            return launch_failure('invocation_scope', ValueError())
        payload = json.dumps({'uncertain_targets': [list(row) for row in rows]}).encode('utf-8')
        if len(payload) > MAX_SCOPE_BYTES:
            return launch_failure('invocation_scope', ValueError())
        argv.append('--invocation-scope')
        options['input'] = payload
    try:
        if on_event is None:
            child = subprocess.run(
                argv, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=timeout, check=False, **options,
            )  # fmt: skip
        else:
            child = run_streamed(
                argv, cwd=project, on_event=on_event, progress_env=PROGRESS_ENV,
                timeout=timeout, on_stderr=on_stderr, **options,
            )  # fmt: skip
    except subprocess.TimeoutExpired as error:
        partial = latest_summary(error.stdout) if error.stdout is not None else None
        merge_run_scope(run_scope, partial)
        result = summarize(command, target_day, 1, partial) if partial is not None else {}
        return {
            **result,
            'exit_code': None,
            'execution': 'timeout',
            'failure_stage': 'execute',
            'error_type': 'TimeoutExpired',
        }
    except OSError as error:
        stage = 'project_cwd' if error.filename in (project, str(project)) else 'process_start'
        return launch_failure(stage, error)
    summary = latest_summary(child.stdout)
    merge_run_scope(run_scope, summary)
    return summarize(command, target_day, child.returncode, summary)


def run_scheduled(
    command, target_day, *, project=None, private=None, timeout=None, run_scope=None,
    on_event=None, python=None,
):  # fmt: skip
    """Run one phase; return ``(record, saved path or None)``."""
    if command not in COMMANDS or iso_day(target_day) is None:
        raise ValueError('invalid scheduled arguments')
    project = Path(project) if project is not None else DEFAULT_PROJECT
    private = Path(private) if private is not None else DEFAULT_PRIVATE
    started = datetime.now(BEIJING)
    options = {'on_event': on_event} if on_event is not None else {}
    outcome = run_child(
        project, command, target_day, timeout, run_scope=run_scope, python=python,
        on_stderr=lambda data: storage.keep_stderr(private, started, command, data), **options,
    )  # fmt: skip
    record = {
        'command': command,
        'civil_date': started.date().isoformat(),
        'started_at': started.isoformat(timespec='seconds'),
        'finished_at': datetime.now(BEIJING).isoformat(timespec='seconds'),
        **outcome,
    }
    try:
        path = storage.archive(private, started, command, record)
    except OSError as error:
        # Business requests already happened. Keep their in-memory deltas even
        # if the optional result file cannot be saved; never replay the phase.
        record.update(
            result_storage='failed',
            result_storage_error_type=allowed(type(error).__name__, ERROR_TYPES, 'OtherError'),
        )
        return record, None
    return record, path


def _archive(private, started, command, record) -> None:
    try:
        storage.archive(private, started, command, record)
    except OSError as error:
        record.update(
            result_storage='failed',
            result_storage_error_type=allowed(type(error).__name__, ERROR_TYPES, 'OtherError'),
        )


def round_command_line(commands, target_day, *, python=None) -> list[str]:
    return [
        python or sys.executable, '-B', '-u', 'main.py', 'run', '--phases', ','.join(commands),
        '--not-before-game-day', target_day, '--scheduled-result',
    ]  # fmt: skip


def run_round(
    commands, target_day, *, project=None, private=None, timeout=None, run_scope=None,
    on_event=None, on_phase=None, python=None,
):  # fmt: skip
    """Run several phases in one worker process that shares its logins.

    The worker (``main.py run``) prints ``{"phase", "exit_code", "logins",
    "result"}`` when each phase ends. ``on_phase(command, record)`` receives the record
    :func:`run_scheduled` would have returned for that phase, as soon as the
    phase ends and in order; the record is archived the same way. ``timeout``
    bounds each phase. Returns the commands that reported: the caller runs the
    rest on their own. A phase that may have started is reported (failed or
    timed out) rather than left for the caller, so nothing is replayed.
    """
    commands = tuple(commands)
    if (
        not commands
        or len(set(commands)) != len(commands)
        or any(name not in COMMANDS for name in commands)
        or iso_day(target_day) is None
    ):
        raise ValueError('invalid round arguments')  # fmt: skip
    project = Path(project) if project is not None else DEFAULT_PROJECT
    private = Path(private) if private is not None else DEFAULT_PRIVATE
    try:
        if not stat.S_ISDIR(project.stat().st_mode):
            return ()
    except OSError:
        return ()  # the caller's own runs report the missing checkout
    argv = round_command_line(commands, target_day, python=python)
    options = {}
    if run_scope is not None:
        rows = _scope_rows(run_scope)
        if rows is None:
            return ()
        payload = json.dumps({'uncertain_targets': [list(row) for row in rows]}).encode('utf-8')
        if len(payload) > MAX_SCOPE_BYTES:
            return ()
        argv.append('--invocation-scope')
        options['input'] = payload
    reported = []
    state = {'started': datetime.now(BEIJING), 'lines': 0}
    round_started = state['started']

    def finish(outcome) -> None:
        command = commands[len(reported)]
        record = {
            'command': command,
            'civil_date': state['started'].date().isoformat(),
            'started_at': state['started'].isoformat(timespec='seconds'),
            'finished_at': datetime.now(BEIJING).isoformat(timespec='seconds'),
            **outcome,
        }
        _archive(private, state['started'], command, record)
        reported.append(command)
        state['started'] = datetime.now(BEIJING)
        if on_phase is not None:
            on_phase(command, record)

    def line(raw: bytes) -> None:
        state['lines'] += 1
        if len(reported) >= len(commands):
            return
        try:
            row = json.loads(raw)
        except ValueError:
            return
        if type(row) is not dict or row.get('phase') != commands[len(reported)]:
            return
        summary = row.get('result') if type(row.get('result')) is dict else None
        code = row.get('exit_code')
        code = code if type(code) is int and 0 <= code <= 255 else 1
        merge_run_scope(run_scope, summary)
        outcome = summarize(commands[len(reported)], target_day, code, summary)
        logins = row.get('logins')
        if type(logins) is int and 0 <= logins <= 1000:
            outcome['session_logins'] = logins
        finish(outcome)

    try:
        child = run_streamed(
            argv, cwd=project, on_event=on_event or (lambda event: None),
            progress_env=PROGRESS_ENV, timeout=timeout, on_line=line,
            restart_timeout_on_line=True,
            on_stderr=lambda data: storage.keep_stderr(private, round_started, 'round', data),
            **options,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        if len(reported) < len(commands):
            finish({'exit_code': None, 'execution': 'timeout', 'failure_stage': 'execute',
                    'error_type': 'TimeoutExpired'})  # fmt: skip
        return tuple(reported)
    except OSError:
        return tuple(reported)
    if len(reported) < len(commands):
        if not reported and not state['lines'] and child.returncode == 2:
            return ()  # a worker without ``run`` refused the arguments; nothing started
        finish(summarize(commands[len(reported)], target_day, child.returncode, None))
    return tuple(reported)
