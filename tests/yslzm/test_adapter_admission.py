"""Admission of the yslzm adapter: static conformance with the worker source, plus a read-only run."""

from pathlib import Path
import textwrap

import pytest

from autocontrol.admission import run_adapter_check
from autocontrol.projects.yslzm import YslzmProject
from autocontrol.projects.yslzm.conformance import check, upstream_jobs

from .test_project import FAKE_MAIN

YSLZM = Path.home() / 'Documents' / 'workspace' / 'yslzm'


def criteria(items):
    return {item['id']: item['passed'] for item in items}


@pytest.mark.skipif(
    not (YSLZM / 'business' / 'progress_events.py').exists(), reason='no yslzm checkout'
)
def test_adapter_understands_everything_the_current_worker_can_report():
    failed = [
        item
        for item in check(YSLZM, phase_timeout=YslzmProject.phase_timeout)
        if not item['passed']
    ]
    assert not failed, failed


def fake_upstream(
    root: Path,
    *,
    tasks='arena cardbook',
    states='complete weird_state',
    jobs="('gold_purchase',x),('mystery_job',y)",
):
    business = root / 'business'
    business.mkdir(parents=True)
    (business / 'progress_events.py').write_text(
        textwrap.dedent(
            f'''
        TASKS = frozenset({tasks.split()!r} + ['mystery'])
        ALIASES = {{'old_name': 'arena'}}
        STATES = frozenset({states.split()!r})
        REASONS = frozenset(['action_exception', 'brand_new_reason'])
        UNITS = frozenset(['次', '桶'])
    '''
        )
    )
    (business / 'additional_daily_policies.py').write_text(
        textwrap.dedent(
            f'''
        def run(command):
            if command=='craft':
                jobs=({jobs})
            elif command=='wardrobe':
                jobs=(('palette_unlock',a),('qianhuan',b))
            else:raise ValueError('invalid additional phase')
            for name,run in jobs:
                pass
    '''
        )
    )
    (business / 'module_registry.py').write_text(
        "from types import SimpleNamespace as NS\n"
        "MODULES = (NS(section='cardbook', schedule=('06:00', '12:00')),)\n"
    )


def test_each_static_check_detects_drift(tmp_path):
    fake_upstream(tmp_path)
    result = criteria(check(tmp_path, phase_timeout=None))
    assert result == {
        'Y1': False, 'Y2': False, 'Y3': False, 'Y4': False, 'Y5': False, 'Y6': False, 'Y7': False,
    }  # fmt: skip
    details = {item['id']: item['detail'] for item in check(tmp_path, phase_timeout=None)}
    assert 'mystery' in details['Y1'] and 'weird_state' in details['Y2']
    assert 'brand_new_reason' in details['Y3'] and '桶' in details['Y4']
    assert (
        'craft.mystery_job' in details['Y5'] and 'craft.stamina_purchase 执行器已不再运行' in details['Y5']
    )
    assert '注册表 06:00、12:00' in details['Y7'] and '控制台 06:00、14:00、22:00' in details['Y7']


def test_job_tables_are_parsed_per_phase():
    source = (
        "def f(command):\n    if command=='craft':\n        jobs=(('a',lambda:g('gold')),\n"
        "            ('b',h))\n    elif command=='activities':\n        x=1\n        jobs=(('c',i),('d',j))\n"
        "    else:raise ValueError('x')\n    for name,run in jobs:\n        pass\n"
    )
    assert upstream_jobs(source) == {'craft': ['a', 'b'], 'activities': ['c', 'd']}


def test_read_only_run_is_graded(project, tmp_path):
    (project.workdir / 'main.py').write_text(FAKE_MAIN)
    report = run_adapter_check(project, save=False)
    live = {item['id']: item['passed'] for item in report['criteria'] if item['id'].startswith('L')}
    assert live == {'L1': True, 'L2': True, 'L3': True, 'L4': True}


def test_read_only_run_reports_unknown_tasks_and_states(project):
    noisy = FAKE_MAIN.replace(
        "summary = {'game_day': day}",
        "emit({'kind': 'task_finished', 'task_id': 'mystery_task', 'status': 'complete'})\n"
        "emit({'kind': 'task_finished', 'task_id': 'arena', 'status': 'weird_state'})\nsummary = {'game_day': day}",
    )
    (project.workdir / 'main.py').write_text(noisy)
    report = run_adapter_check(project, save=False)
    l3 = next(item for item in report['criteria'] if item['id'] == 'L3')
    assert not l3['passed'] and 'mystery_task' in l3['detail'] and 'weird_state' in l3['detail']
