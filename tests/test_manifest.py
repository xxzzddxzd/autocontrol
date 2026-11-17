"""Protocol projects described by a manifest, run against real child processes."""

import json
import os
import textwrap

import pytest

from autocontrol.metrics import MetricsStore
from autocontrol.projects.manifest import ManifestError, ManifestProject, validate_manifest
from autocontrol.runner import RoundRunner

from .helpers import DEMO_MANIFEST, demo_copy


class Publisher:
    status = {'status': 'sent'}

    def __init__(self):
        self.snapshots = []

    def submit(self, snapshot):
        self.snapshots.append(snapshot)

    def flush(self, timeout):
        return True


def project_for(tmp_path, **changes):
    manifest = demo_copy(tmp_path, **changes)
    private = tmp_path / 'private'
    private.mkdir(mode=0o700, exist_ok=True)
    return ManifestProject(manifest)


def tasks(snapshot):
    return {row['task_id']: row for row in snapshot['tasks']}


def test_example_manifest_is_valid():
    assert validate_manifest(json.loads(DEMO_MANIFEST.read_text())) == []


def test_manifest_problems_are_all_reported():
    data = json.loads(DEMO_MANIFEST.read_text())
    data.update(protocol=2, name='Bad Name', command=['worker.py'], phases=[], extra=1)
    data['tasks'].append({'id': 'signin', 'title': 'dup'})
    problems = validate_manifest(data)
    for fragment in ('protocol must be 1', 'name must match', 'command must', 'phases must', 'unknown field: extra',
                     "task id 'signin'"):  # fmt: skip
        assert any(fragment in problem for problem in problems), fragment
    with pytest.raises(ManifestError):
        ManifestProject(DEMO_MANIFEST.with_name('missing.json'))


def test_catalog_schedule_and_locations_come_from_the_manifest(tmp_path):
    project = project_for(tmp_path)
    assert project.name == 'demo' and project.phases == ('daily', 'shop')
    assert [s.key for s in project.catalog.sections] == ['daily', 'shop']
    assert project.catalog.tasks['mail_read'].section == 'daily'
    assert project.schedule.tz_name == 'Asia/Shanghai' and project.schedule.times[0] == '06:00'
    assert project.account_lock is None and project.private == tmp_path / 'private'
    assert project.workdir == (tmp_path / 'demo').resolve()
    assert project.argv('daily', 'execute', '2026-11-17')[-1] == 'daily'


def test_round_updates_the_card_archives_results_and_records_metrics(tmp_path):
    project = project_for(tmp_path)
    publisher = Publisher()
    metrics = MetricsStore(project.metrics_db)
    result = RoundRunner(project, publisher, metrics=metrics).run_once()
    assert (result['status'], result['code'], result['phases']) == ('finished', 0, 2)
    final = tasks(publisher.snapshots[-1])
    assert final['signin']['status'] == 'complete'
    assert final['mail']['status'] == 'complete' and final['mail_read']['status'] == 'complete'
    assert (final['energy']['status'], final['energy']['current'], final['energy']['total']) == (
        'waiting',
        1,
        3,
    )
    assert (final['weekly_pack']['status'], final['weekly_pack']['reason']) == (
        'blocked',
        'insufficient_currency',
    )
    assert final['limited_event']['status'] == 'closed'
    card = json.dumps(project.render(publisher.snapshots[-1]), ensure_ascii=False)
    assert '每日签到' in card and '货币不足' in card and '限时活动' not in card
    saved = sorted((project.private / 'results').glob('*/*.json'))
    assert len(saved) == 2
    record = json.loads(saved[0].read_text())
    assert record['protocol'] == {'version': 1, 'events': record['protocol']['events'], 'events_rejected': 0,
                                  'result_valid': True, 'violations': [], 'ignored_fields': 0}  # fmt: skip
    report = metrics.report(
        'demo', today=project.schedule.service_day(), phase_timeout=project.phase_timeout
    )
    assert report['phases']['daily']['actions_confirmed'] == 3
    assert report['indicators']['protocol_violations'] == 0


def test_read_only_discovery_applies_the_final_plan(tmp_path):
    project = project_for(tmp_path)
    publisher = Publisher()
    result = RoundRunner(project, publisher).run_once(observe_only=True)
    assert (result['status'], result['code']) == ('observed', 0)
    final = publisher.snapshots[-1]
    assert tasks(final)['weekly_pack']['status'] == 'blocked'
    assert final['dynamic_source'] == 'server_observation'
    assert not (project.private / 'results').exists()  # discovery is not archived


WORKERS = {
    'busy': 'import sys\nsys.exit(2)\n',
    'no_result': 'print("hello")\n',
    'bad_result': 'import json\nprint(json.dumps({"v": 1, "phase": "daily", "day": "2026-11-17", "outcome": "done"}))\n',
    'crash': 'raise SystemExit(5)\n',
    'slow': 'import time\ntime.sleep(30)\n',
}


@pytest.mark.parametrize('name, execution, stage, code', [
    ('busy', 'busy', 'lock', 2),
    ('no_result', 'unverified', 'protocol', 1),
    ('bad_result', 'unverified', 'protocol', 1),
    ('crash', 'failed', 'process', 5),
])  # fmt: skip
def test_worker_faults_become_fixed_records(tmp_path, name, execution, stage, code):
    project = project_for(tmp_path)
    (project.workdir / 'worker.py').write_text(WORKERS[name])
    record, _ = project.invoke('daily', 'execute', '2026-11-17')
    assert (record['execution'], record['failure_stage'], project.exit_code(record)) == (
        execution,
        stage,
        code,
    )


def test_timeout_is_recorded_and_the_child_is_stopped(tmp_path):
    project = project_for(tmp_path)
    (project.workdir / 'worker.py').write_text(WORKERS['slow'])
    record, _ = project.invoke('daily', 'execute', '2026-11-17', timeout=0.5)
    assert (record['execution'], record['failure_stage'], record['exit_code']) == (
        'timeout',
        'timeout',
        None,
    )


def test_run_scope_passes_between_phases_in_memory_only(tmp_path):
    source = textwrap.dedent(
        '''
        import json, sys
        sys.path.insert(0, '.')
        from autocontrol_worker import Worker
        worker = Worker()
        seen = list(worker.run_scope.get('seen', []))
        worker.finish('complete', {}, run_scope={'seen': seen + [worker.phase]})
        print(json.dumps({'stdin': seen}), file=sys.stderr)
        '''
    )
    project = project_for(tmp_path, run_scope=True)
    (project.workdir / 'worker.py').write_text(source)
    scope = project.new_run_scope()
    project.run_phase('daily', '2026-11-17', run_scope=scope, on_event=lambda e: None)
    project.run_phase('shop', '2026-11-17', run_scope=scope, on_event=lambda e: None)
    assert scope == {'seen': ['daily', 'shop']}
    saved = ''.join(path.read_text() for path in (project.private / 'results').glob('*/*.json'))
    assert 'seen' not in saved


def test_worker_receives_the_protocol_environment(tmp_path):
    source = 'import json, os\nprint(json.dumps({k: v for k, v in os.environ.items() if k.startswith("AUTOCONTROL_")}))\n'
    project = project_for(tmp_path)
    (project.workdir / 'worker.py').write_text(source)
    from autocontrol.worker import run_streamed

    child = run_streamed(project.argv('daily', 'execute', '2026-11-17'), cwd=project.workdir, on_event=lambda _: None,
                         progress_env='AUTOCONTROL_PROGRESS_FD', env={'AUTOCONTROL_PHASE': 'daily'}, raw=True)  # fmt: skip
    env = json.loads(child.stdout)
    assert env['AUTOCONTROL_PHASE'] == 'daily' and env['AUTOCONTROL_PROGRESS_FD'].isdigit()
    record, _ = project.invoke('daily', 'observe', '2026-11-17')
    assert record['failure_stage'] == 'protocol'  # printing the env is not a protocol result
    assert os.environ.get('AUTOCONTROL_PHASE') is None


def test_demand_endpoint_and_discovery_are_validated(tmp_path):
    tasks = [
        {
            'id': 'signin',
            'title': '每日签到',
            'section': 'daily',
            'demand': 'daily',
            'endpoint': 'claim',
        },
        {
            'id': 'event',
            'title': '限时活动',
            'section': 'daily',
            'demand': 'window',
            'endpoint': 'once',
        },
        {'id': 'radar', 'title': '开放活动', 'section': 'daily', 'discovery': True},
    ]
    catalog = project_for(tmp_path, tasks=tasks).catalog
    assert (catalog.tasks['event'].demand, catalog.tasks['event'].endpoint) == ('window', 'once')
    assert catalog.tasks['radar'].discovery and not catalog.tasks['signin'].discovery
    data = json.loads(DEMO_MANIFEST.read_text())
    data['tasks'] = tasks + [
        {'id': 'radar_two', 'title': '开放活动二', 'section': 'daily', 'discovery': True},
        {'id': 'odd', 'title': '怪', 'section': 'daily', 'demand': 'monthly', 'endpoint': 'forever'},
    ]
    problems = '；'.join(validate_manifest(data))
    assert 'demand must be one of' in problems and 'endpoint must be one of' in problems
    assert 'at most one task may be the discovery task' in problems
