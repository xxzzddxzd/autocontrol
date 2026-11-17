"""The vendored Python SDK emits protocol-conformant events and results."""

import importlib.util
import io
import json
import os

import pytest

from autocontrol import protocol
from autocontrol.projects.manifest import ManifestProject

from .helpers import DEMO_MANIFEST, SDK

spec = importlib.util.spec_from_file_location('autocontrol_worker', SDK)
sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sdk)


@pytest.fixture
def pipe():
    read_fd, write_fd = os.pipe()
    yield read_fd, write_fd
    for fd in (read_fd, write_fd):
        try:
            os.close(fd)
        except OSError:
            pass


def lines(fd):
    os.set_blocking(fd, False)
    try:
        data = os.read(fd, 1 << 16)
    except BlockingIOError:
        data = b''
    return [line for line in data.split(b'\n') if line]


def test_events_and_result_pass_the_console_validators(pipe):
    read_fd, write_fd = pipe
    out = io.StringIO()
    env = {'AUTOCONTROL_PROTOCOL': '1', 'AUTOCONTROL_PHASE': 'daily', 'AUTOCONTROL_MODE': 'execute',
           'AUTOCONTROL_SERVICE_DAY': '2026-11-17', 'AUTOCONTROL_PROGRESS_FD': str(write_fd)}  # fmt: skip
    worker = sdk.Worker(environ=env, stdin=io.StringIO('{"run_scope": {"a": 1}}'), stdout=out)
    assert worker.run_scope == {'a': 1} and not worker.observe
    worker.plan({'signin': {'status': 'not_started'}}, next_wake=[2_000_000_000])
    with worker.task('signin') as run:
        worker.count('actions_sent').count('actions_confirmed')
        run.finish('complete')
    with pytest.raises(RuntimeError):
        with worker.task('mail'):
            raise RuntimeError('boom')
    worker.progress('energy', current=1, total=3)
    worker.emit('plan', tasks={'x': 'y' * 5000})  # oversized: dropped, never raises
    result = worker.finish('partial', {'signin': {'status': 'complete'}})
    catalog = ManifestProject(DEMO_MANIFEST, workdir='/tmp', private='/tmp/x').catalog
    report = protocol.Violations()
    events = [protocol.parse_event_line(line, catalog, 'daily', report) for line in lines(read_fd)]
    assert not report and [e['kind'] for e in events] == [
        'plan',
        'task_started',
        'task_finished',
        'task_started',
        'task_failed',
        'task_progress',
    ]
    assert [e['sequence'] for e in events] == [1, 2, 3, 4, 5, 6]
    printed = json.loads(out.getvalue().strip().splitlines()[-1])
    assert printed == result
    assert protocol.parse_result(printed, catalog, 'daily', report, allow_scope=False)[
        'metrics'
    ] == {'actions_sent': 1, 'actions_confirmed': 1}
    assert not report


def test_works_outside_the_console():
    out = io.StringIO()
    worker = sdk.Worker(environ={}, stdin=io.StringIO(''), stdout=out)
    assert worker.emit('plan', tasks={}) is False
    worker.finish('complete', {})
    assert json.loads(out.getvalue().strip())['outcome'] == 'complete'
    assert sdk.Worker.exit_code('busy') == 2 and sdk.Worker.exit_code('partial') == 0


def test_schemas_accept_the_example_manifest_and_sdk_output(pipe):
    jsonschema = pytest.importorskip('jsonschema')
    from autocontrol import schemas

    jsonschema.validate(json.loads(DEMO_MANIFEST.read_text()), schemas.manifest_schema())
    read_fd, write_fd = pipe
    out = io.StringIO()
    worker = sdk.Worker(environ={'AUTOCONTROL_PROTOCOL': '1', 'AUTOCONTROL_PHASE': 'daily',
                                 'AUTOCONTROL_PROGRESS_FD': str(write_fd)}, stdin=io.StringIO(''), stdout=out)  # fmt: skip
    worker.plan(
        {'signin': {'status': 'waiting', 'next_run_at': 2_000_000_000, 'counts': {'claimed': 1}}}
    )
    with worker.task('signin'):
        worker.count('reads')
    worker.failed('mail', status='result_unavailable', reason='request_result_missing')
    worker.finish('waiting', {'signin': {'status': 'complete'}}, next_wake=[2_000_000_000])
    for line in lines(read_fd):
        jsonschema.validate(json.loads(line), schemas.event_schema())
    jsonschema.validate(
        json.loads(out.getvalue().strip().splitlines()[-1]), schemas.result_schema()
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {'v': 1, 'kind': 'task_finished', 'seq': 1, 'ts': 1, 'task': 'x'},
            schemas.event_schema(),
        )


def test_a_task_never_assumes_complete(pipe):
    read_fd, write_fd = pipe
    worker = sdk.Worker(environ={'AUTOCONTROL_PROTOCOL': '1', 'AUTOCONTROL_PHASE': 'daily',
                                 'AUTOCONTROL_PROGRESS_FD': str(write_fd)},
                        stdin=io.StringIO(''), stdout=io.StringIO())  # fmt: skip
    with worker.task('signin'):
        pass  # the module forgot to report what it read back
    with worker.task('mail') as run:
        run.finish('waiting', reason='waiting_for_completion')
    with pytest.raises(ValueError):
        with worker.task('energy') as run:
            run.finish('complete')
            raise ValueError('cleanup failed after the server confirmed')
    with worker.task('signin') as run:
        run.fail('request_result_missing', status='result_unavailable')
        with pytest.raises(RuntimeError):
            run.finish('complete')  # one outcome per task
    events = [json.loads(line) for line in lines(read_fd)]
    reported = [(e['task'], e['kind'], e['status'], e.get('reason'))
                for e in events if e['kind'] != 'task_started']  # fmt: skip
    assert reported == [
        ('signin', 'task_finished', 'result_unavailable', 'ended_without_result'),
        ('mail', 'task_finished', 'waiting', 'waiting_for_completion'),
        ('energy', 'task_finished', 'complete', None),
        ('signin', 'task_failed', 'result_unavailable', 'request_result_missing'),
    ]
    catalog = ManifestProject(DEMO_MANIFEST, workdir='/tmp', private='/tmp/x').catalog
    report = protocol.Violations()
    for event in events:
        protocol.parse_event_line(json.dumps(event).encode(), catalog, 'daily', report)
    assert not report
