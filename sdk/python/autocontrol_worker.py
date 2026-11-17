"""autocontrol worker protocol v1 — single-file helper for Python scripts.

Copy this file next to your script (stdlib only, Python 3.8+). It reads the
environment the console provides, streams progress events safely and prints
the final result as the last stdout line. Spec: docs/接入规范.md.

    from autocontrol_worker import Worker

    worker = Worker()
    worker.plan({'signin': {'status': 'incomplete'}})
    if not worker.observe:
        with worker.task('signin') as run:
            sign_in()                       # raises -> task_failed (action_exception)
            worker.count('actions_sent').count('actions_confirmed')
            run.finish('complete' if signed_in_on_server() else 'incomplete')
    worker.finish('complete', {'signin': {'status': 'complete'}})

Run outside the console (no AUTOCONTROL_* variables) it still works: events
are dropped and the result is printed, which is handy for local testing.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
import sys
import time
import uuid

VERSION = 1
STATUSES = ('not_started', 'running', 'incomplete', 'waiting', 'blocked', 'complete', 'failed',
            'result_unavailable', 'unsupported', 'closed')  # fmt: skip
OUTCOMES = ('complete', 'partial', 'waiting', 'failed', 'busy', 'skipped')
FAILURE_STAGES = ('config', 'lock', 'login', 'connect', 'read', 'execute', 'write')
METRIC_KEYS = ('actions_sent', 'actions_confirmed', 'actions_failed', 'actions_unconfirmed', 'reads',
               'retries', 'sessions')  # fmt: skip
EXIT_OK, EXIT_FAILED, EXIT_BUSY, EXIT_CONFIG = 0, 1, 2, 3
MAX_EVENT_BYTES = 4096


class TaskRun:
    """One task inside ``with worker.task(...) as run:``; report its outcome once."""

    def __init__(self, worker: Worker, task: str):
        self._worker, self.task, self.status = worker, task, None

    def finish(self, status: str, **state) -> bool:
        """The read-back state: ``complete`` only with server evidence."""
        self._settle(status)
        return self._worker.finished(self.task, status, **state)

    def fail(self, reason: str = 'action_exception', status: str = 'failed', **state) -> bool:
        self._settle(status)
        return self._worker.failed(self.task, reason=reason, status=status, **state)

    def _settle(self, status: str) -> None:
        if self.status is not None:
            raise RuntimeError(f'task {self.task!r} already reported {self.status!r}')
        self.status = status


class Worker:
    def __init__(self, *, environ=None, stdin=None, stdout=None):
        env = os.environ if environ is None else environ
        self.protocol = env.get('AUTOCONTROL_PROTOCOL')
        self.project = env.get('AUTOCONTROL_PROJECT', '')
        self.phase = env.get('AUTOCONTROL_PHASE', sys.argv[1] if len(sys.argv) > 1 else 'manual')
        self.mode = env.get('AUTOCONTROL_MODE', 'execute')
        self.observe = self.mode == 'observe'
        self.service_day = env.get('AUTOCONTROL_SERVICE_DAY') or time.strftime('%Y-%m-%d')
        self.run_id = env.get('AUTOCONTROL_RUN_ID') or uuid.uuid4().hex
        deadline = env.get('AUTOCONTROL_DEADLINE', '')
        self.deadline = float(deadline) if deadline.replace('.', '', 1).isdigit() else None
        self._run = uuid.uuid4().hex[:16]
        self._seq = 0
        self._stdout = stdout or sys.stdout
        self.metrics = {}
        self._fd = None
        value = env.get('AUTOCONTROL_PROGRESS_FD', '')
        if value.isdigit():
            try:
                os.set_blocking(int(value), False)
                self._fd = int(value)
            except OSError:
                self._fd = None
        self.run_scope = {}
        source = stdin if stdin is not None else sys.stdin
        if self.protocol and source is not None and not source.isatty():
            try:
                data = source.read()
                parsed = json.loads(data) if data.strip() else {}
                if isinstance(parsed, dict) and isinstance(parsed.get('run_scope'), dict):
                    self.run_scope = parsed['run_scope']
            except (ValueError, OSError):
                pass

    # -- time ---------------------------------------------------------------

    def time_left(self) -> float | None:
        """Seconds until the console's soft deadline (stop starting new work at <= 0)."""
        return None if self.deadline is None else self.deadline - time.time()

    # -- events -----------------------------------------------------------------

    def emit(self, kind: str, **fields) -> bool:
        """Send one event; never blocks and never raises (events are best effort)."""
        if self._fd is None:
            return False
        self._seq += 1
        event = {
            'v': VERSION,
            'kind': kind,
            'seq': self._seq,
            'run': self._run,
            'ts': time.time(),
            **fields,
        }
        line = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
        if len(line) > MAX_EVENT_BYTES:
            return False
        try:
            return os.write(self._fd, line) == len(line)
        except OSError as error:
            if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EPIPE):
                self._fd = None
            return False

    def plan(self, tasks: dict, *, next_wake=(), day: str | None = None) -> bool:
        """Report the current state of every task of this phase (read-only)."""
        fields = {'tasks': tasks, 'day': day or self.service_day}
        if next_wake:
            fields['next_wake'] = sorted(set(next_wake))[:48]
        return self.emit('plan', **fields)

    def started(self, task: str) -> bool:
        return self.emit('task_started', task=task, status='running')

    def progress(self, task: str, current=None, total=None, unit=None, **state) -> bool:
        fields = {key: value for key, value in (('current', current), ('total', total), ('unit', unit))
                  if value is not None}  # fmt: skip
        return self.emit('task_progress', task=task, **fields, **state)

    def finished(self, task: str, status: str = 'complete', **state) -> bool:
        return self.emit('task_finished', task=task, status=status, **state)

    def failed(
        self, task: str, reason: str = 'action_exception', status: str = 'failed', **state
    ) -> bool:
        return self.emit('task_failed', task=task, status=status, reason=reason, **state)

    @contextmanager
    def task(self, task: str):
        """``with worker.task('mail') as run:`` … ``run.finish('complete')``.

        Report the state you read back from the server with ``run.finish``
        (e.g. ``run.finish('waiting', reason='waiting_for_completion')``). A
        body that raises reports ``failed`` / ``action_exception``; a body that
        ends without ``finish`` reports ``result_unavailable`` /
        ``ended_without_result``. The SDK never assumes ``complete``.
        """
        run = TaskRun(self, task)
        self.started(task)
        try:
            yield run
        except Exception:
            if run.status is None:
                self.failed(task)
            raise
        if run.status is None:
            self.finished(task, 'result_unavailable', reason='ended_without_result')

    def count(self, metric: str, amount: int = 1) -> Worker:
        if metric in METRIC_KEYS:
            self.metrics[metric] = self.metrics.get(metric, 0) + max(0, int(amount))
        return self

    # -- result --------------------------------------------------------------------

    def finish(self, outcome: str, tasks: dict, *, day: str | None = None, failure_stage=None,
               error_type=None, next_wake=(), run_scope=None) -> dict:  # fmt: skip
        """Print the final result as the last stdout line and return it."""
        result = {'v': VERSION, 'phase': self.phase, 'day': day or self.service_day, 'outcome': outcome,
                  'tasks': tasks, 'metrics': dict(self.metrics)}  # fmt: skip
        if failure_stage:
            result['failure_stage'] = failure_stage
        if error_type:
            result['error_type'] = error_type
        if next_wake:
            result['next_wake'] = sorted(set(next_wake))[:48]
        if run_scope is not None:
            result['run_scope'] = run_scope
        self._stdout.write(
            '\n' + json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n'
        )
        self._stdout.flush()
        return result

    @staticmethod
    def exit_code(outcome: str) -> int:
        return {'failed': EXIT_FAILED, 'busy': EXIT_BUSY}.get(outcome, EXIT_OK)
