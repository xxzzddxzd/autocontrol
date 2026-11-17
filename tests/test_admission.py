"""Admission check: graded against the criteria of docs/接入规范.md."""

import json

from autocontrol.admission import admitted, format_report, run_check
from autocontrol.projects.manifest import ManifestProject

from .helpers import demo_copy


def criteria(report):
    return {item['id']: item['passed'] for item in report['criteria']}


def test_the_demo_worker_passes_and_is_admitted(tmp_path):
    manifest = demo_copy(tmp_path)
    report = run_check(manifest, repeat=2)
    assert report['passed'] and all(criteria(report).values())
    project = ManifestProject(manifest)
    assert admitted(project) == (True, '')
    assert '准入检查：demo  通过' in format_report(report)


def test_changed_manifest_must_be_checked_again(tmp_path):
    manifest = demo_copy(tmp_path)
    run_check(manifest)
    data = json.loads(manifest.read_text())
    data['display_name'] = '示例脚本二'
    manifest.write_text(json.dumps(data, ensure_ascii=False))
    ok, why = admitted(ManifestProject(manifest))
    assert not ok and '重新运行准入检查' in why


def test_invalid_manifest_fails_m1(tmp_path):
    manifest = demo_copy(tmp_path)
    manifest.write_text('{"protocol": 1}')
    report = run_check(manifest)
    assert not report['passed'] and criteria(report) == {'M1': False}


def test_a_worker_that_mutates_in_observe_mode_is_refused(tmp_path):
    source = (
        'import sys\nsys.path.insert(0, ".")\nfrom autocontrol_worker import Worker\n'
        'w = Worker()\nw.plan({"signin": {"status": "complete"}})\nw.count("actions_sent")\n'
        'w.finish("complete", {"signin": {"status": "complete"}})\n'
    )
    report = run_check(demo_copy(tmp_path, worker_source=source))
    result = criteria(report)
    assert not report['passed']
    assert result['M7'] is False and result['M6'] is False and result['M3'] is True


def test_unknown_tasks_and_missing_plan_are_reported(tmp_path):
    source = (
        'import json\nprint(json.dumps({"v": 1, "phase": "status", "day": "2026-11-17", "outcome": "complete",'
        ' "tasks": {"ghost": {"status": "complete"}}, "metrics": {"actions_sent": 0, "reads": 1}}))\n'
    )
    report = run_check(demo_copy(tmp_path, worker_source=source))
    result = criteria(report)
    assert result['M4'] is False and result['M5'] is False and not report['passed']


def test_static_check_never_admits(tmp_path):
    report = run_check(demo_copy(tmp_path), live=False)
    assert criteria(report)['M1'] and not report['passed']


WINDOW_TASKS = [
    {'id': 'signin', 'title': '每日签到', 'section': 'daily'},
    {'id': 'event', 'title': '限时活动', 'section': 'daily', 'demand': 'window', 'endpoint': 'once'},
    {'id': 'radar', 'title': '开放活动', 'section': 'daily', 'discovery': True},
]
WINDOW_WORKER = '''
import sys
from autocontrol_worker import Worker

OPENS = 2_000_000_000
worker = Worker()
tasks = {'signin': {'status': 'complete'},
         'event': {'status': 'waiting', 'reason': 'not_open', 'next_run_at': OPENS},
         'radar': {'status': 'complete', 'counts': {'unsupported': 0}}}
worker.plan({key: value for key, value in tasks.items() if key in KEYS})
worker.count('actions_sent', 0).count('reads')
worker.finish('complete', {key: value for key, value in tasks.items() if key in KEYS}, next_wake=WAKE)
sys.exit(0)
'''


def window_check(tmp_path, *, wake, keys=('signin', 'event', 'radar'), tasks=WINDOW_TASKS):
    source = WINDOW_WORKER.replace('WAKE', wake).replace('KEYS', repr(set(keys)))
    manifest = demo_copy(tmp_path, worker_source=source, tasks=tasks,
                         count_keys=['claimed', 'unsupported'])  # fmt: skip
    return criteria(run_check(manifest, save=False))


def test_a_waiting_window_task_needs_its_opening_time_in_next_wake(tmp_path):
    good = window_check(tmp_path / 'good', wake='[OPENS]')
    assert good['M8'] and good['S5'] and all(good.values())
    late = window_check(tmp_path / 'late', wake='[]')
    assert late['M8'] is False and late['S5'] is True


def test_window_tasks_without_a_discovery_task_are_flagged(tmp_path):
    tasks = WINDOW_TASKS[:2]
    result = window_check(tmp_path, wake='[OPENS]', keys=('signin', 'event'), tasks=tasks)
    assert result['M8'] and result['S5'] is False


def test_projects_without_window_tasks_pass_the_window_checks(tmp_path):
    report = run_check(demo_copy(tmp_path), save=False)
    assert criteria(report)['M8'] and criteria(report)['S5']
