"""Worker protocol v1 validation: anything outside the contract is dropped and counted."""

import json
import time

import pytest

from autocontrol import protocol
from autocontrol.projects.manifest import ManifestProject

from .helpers import DEMO_MANIFEST


@pytest.fixture(scope='module')
def catalog():
    return ManifestProject(DEMO_MANIFEST, workdir='/tmp', private='/tmp/x').catalog


def event(**fields):
    return {'v': 1, 'seq': 1, 'ts': time.time(), **fields}


def parse(value, catalog):
    report = protocol.Violations()
    line = value if isinstance(value, bytes) else json.dumps(value).encode()
    return protocol.parse_event_line(line, catalog, 'daily', report), report


def test_plan_event_becomes_a_baseline_for_every_known_task(catalog):
    internal, report = parse(
        event(
            kind='plan',
            day='2026-11-17',
            next_wake=[time.time() + 60],
            tasks={
                'signin': {'status': 'complete'},
                'energy': {'status': 'waiting', 'current': 1, 'total': 3},
            },
        ),
        catalog,
    )
    assert not report
    plan = internal['plan']
    assert plan['game_day'] == '2026-11-17' and plan['dynamic_candidates']
    assert plan['full_checklist_summary']['goals']['energy'] == {
        'status': 'waiting',
        'current': 1,
        'total': 3,
    }


def test_task_events_keep_only_contract_fields(catalog):
    internal, report = parse(
        event(
            kind='task_progress',
            task='energy',
            current=2,
            total=3,
            unit='次',
            counts={'claimed': 1},
            token='secret-value',
        ),
        catalog,
    )
    assert internal['task_id'] == 'energy' and internal['counts'] == {'claimed': 1}
    assert 'token' not in json.dumps(internal) and 'secret-value' not in json.dumps(internal)
    assert report.ignored == ['task_progress.token'] and not report


def test_task_failed_with_unknown_result_keeps_that_status(catalog):
    internal, _ = parse(
        event(
            kind='task_failed',
            task='signin',
            status='result_unavailable',
            reason='request_result_missing',
        ),
        catalog,
    )
    assert (internal['kind'], internal['status']) == ('task_finished', 'result_unavailable')
    internal, _ = parse(event(kind='task_failed', task='signin'), catalog)
    assert (internal['kind'], internal['status']) == ('task_failed', 'failed')


@pytest.mark.parametrize('bad, code', [
    (b'not json', 'event_not_json'),
    (b'x' * 5000, 'event_too_large'),
    ({'v': 2, 'kind': 'plan', 'seq': 1, 'ts': 1}, 'event_version'),
    ({'v': 1, 'kind': 'log', 'seq': 1, 'ts': 1}, 'event_kind'),
    ({'v': 1, 'kind': 'plan', 'seq': 0, 'ts': 1}, 'event_seq'),
    ({'v': 1, 'kind': 'task_started', 'seq': 1, 'ts': 1, 'task': 'nope'}, 'task_unknown'),
    ({'v': 1, 'kind': 'task_finished', 'seq': 1, 'ts': 1, 'task': 'signin'}, 'status_missing'),
])  # fmt: skip
def test_invalid_events_are_rejected(catalog, bad, code):
    internal, report = parse(bad, catalog)
    assert internal is None
    assert report.errors[0].startswith(code)


def test_invalid_values_are_dropped_and_counted(catalog):
    internal, report = parse(
        event(
            kind='task_progress',
            task='signin',
            status='done',
            reason='free text',
            current=-1,
            unit='lbs',
            next_run_at='soon',
            counts={'gold': 5},
        ),
        catalog,
    )
    assert set(internal) == {'kind', 'phase', 'sequence', 'generation', 'observed_at', 'task_id'}
    codes = {error.split('@')[0] for error in report.errors}
    assert codes == {
        'status_unknown',
        'reason_unknown',
        'number_invalid',
        'unit_unknown',
        'time_invalid',
        'count_invalid',
    }
    # A finished task without a valid status is rejected as a whole.
    assert parse(event(kind='task_finished', task='signin', status='done'), catalog)[0] is None


def test_result_validation(catalog):
    report = protocol.Violations()
    result = protocol.parse_result({
        'v': 1, 'phase': 'daily', 'day': '2026-11-17', 'outcome': 'partial', 'failure_stage': 'execute',
        'error_type': 'TimeoutError', 'tasks': {'signin': {'status': 'complete'}, 'ghost': {}},
        'metrics': {'actions_sent': 2, 'secret': 1}, 'run_scope': {'k': 1}, 'debug': 'x'}, catalog, 'daily', report,
        allow_scope=True)  # fmt: skip
    assert result['tasks'] == {'signin': {'status': 'complete'}}
    assert result['metrics'] == {'actions_sent': 2} and result['run_scope'] == {'k': 1}
    assert {error.split('@')[0] for error in report.errors} == {'task_unknown', 'metric_invalid'}
    assert report.ignored == ['result.debug']
    for broken in (
        {'v': 1, 'phase': 'daily', 'day': 'today', 'outcome': 'complete'},
        {'v': 1, 'phase': 'daily', 'day': '2026-11-17', 'outcome': 'great'},
        [],
    ):
        assert (
            protocol.parse_result(
                broken, catalog, 'daily', protocol.Violations(), allow_scope=False
            )
            is None
        )


def test_vocabulary_is_documented():
    assert set(protocol.STATUS_TEXT) == set(protocol.STATUSES)
    assert set(protocol.WORKER_FAILURE_STAGES) <= set(protocol.FAILURE_STAGES)
    assert set(protocol.STANDARD_SHORT_REASONS) <= set(protocol.STANDARD_REASONS)


def test_exported_schemas_match_the_code_and_accept_the_sdk_output():
    from autocontrol import schemas
    from .helpers import ROOT

    for name in schemas.SCHEMAS:
        assert (ROOT / 'docs' / 'schemas' / name).read_text(encoding='utf-8') == schemas.render(
            name
        ), f'run: python run.py schema --out docs/schemas ({name} is stale)'


def test_outdated_local_config_is_a_standard_reason():
    from autocontrol import protocol

    assert protocol.STANDARD_REASONS['config_outdated'] == '本地配置落后于游戏版本'
    assert protocol.STANDARD_SHORT_REASONS['config_outdated'] == '配置待更新'


def test_a_protocol_project_may_report_an_outdated_local_config(tmp_path):
    from .helpers import demo_copy

    project = ManifestProject(demo_copy(tmp_path))
    report = protocol.Violations()
    state = protocol.task_state(
        {'status': 'blocked', 'reason': 'config_outdated'}, project.catalog, report, 't'
    )
    assert state == {'status': 'blocked', 'reason': 'config_outdated'} and report.errors == []
    assert project.catalog.short_reasons['config_outdated'] == '配置待更新'
