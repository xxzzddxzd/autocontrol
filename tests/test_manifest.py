"""Protocol projects described by a manifest, run against real child processes."""

import json
import os
import textwrap

import pytest

from autocontrol.lark.store import BoardStore
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
    assert catalog.round_tasks == {}
    harvest = {'id': 'income', 'title': '城镇收益', 'section': 'daily', 'demand': 'round'}
    catalog = project_for(tmp_path / 'round', tasks=[*tasks, harvest]).catalog
    assert catalog.round_tasks == {'income': frozenset()}
    data = json.loads(DEMO_MANIFEST.read_text())
    data['tasks'] = tasks + [
        {'id': 'radar_two', 'title': '开放活动二', 'section': 'daily', 'discovery': True},
        {'id': 'odd', 'title': '怪', 'section': 'daily', 'demand': 'monthly', 'endpoint': 'forever'},
    ]
    problems = '；'.join(validate_manifest(data))
    assert 'demand must be one of' in problems and 'endpoint must be one of' in problems
    assert 'at most one task may be the discovery task' in problems


FAILED_LOGIN = '''
import sys
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parent)]
from autocontrol_worker import Worker
worker = Worker()
keys = ['weekly_pack'] if worker.phase == 'shop' else ['signin', 'mail', 'energy']
tasks = {key: {'status': 'result_unavailable', 'reason': 'action_exception'} for key in keys}
worker.plan(tasks)
for metric in ('actions_sent', 'reads', 'sessions'):
    worker.count(metric, 0)
sys.stderr.write('Traceback (most recent call last):\\nJSONDecodeError: Expecting value\\n')
worker.finish('failed', tasks, failure_stage='login', error_type='JSONDecodeError')
raise SystemExit(1)
'''


def test_a_login_that_failed_before_any_request_keeps_the_days_card(tmp_path):
    project = project_for(tmp_path)
    first = Publisher()
    RoundRunner(project, first).run_once()
    known = first.snapshots[-1]
    BoardStore(project.board_db, sanitize=project.sanitize).save(
        project.name, known['service_day'], 'oc_test', snapshot=known
    )
    (project.workdir / 'worker.py').write_text(FAILED_LOGIN)
    second = Publisher()
    result = RoundRunner(project, second).run_once()
    assert (result['status'], result['code']) == ('finished', 1)
    final = second.snapshots[-1]

    def status(snapshot):
        return {key: row['status'] for key, row in tasks(snapshot).items()}

    assert status(final) == status(known)
    assert {row['phase']: row['status'] for row in final['phases']} == {
        'daily': 'failed',
        'shop': 'failed',
    }
    logs = sorted((project.private / 'logs').glob('*/*.stderr.log'))
    assert [path.name.split('-', 2)[2] for path in logs] == ['daily.stderr.log', 'shop.stderr.log']
    assert b'JSONDecodeError: Expecting value' in logs[0].read_bytes()


SHOP_OWN = {'phases': {'shop': {'times': ['07:00', '15:00']}}}


def own_schedule(**phases):
    data = json.loads(DEMO_MANIFEST.read_text())
    return {**data['schedule'], 'phases': phases}


def test_a_phase_may_keep_its_own_times():
    data = json.loads(DEMO_MANIFEST.read_text())
    assert validate_manifest({**data, 'schedule': own_schedule(shop={'times': ['07:00']})}) == []
    for bad, fragment in (
        ({'ghost': {'times': ['07:00']}}, 'is not a listed phase'),
        ({'shop': {'times': []}}, 'schedule.phases.shop must be'),
        ({'shop': {'times': ['07:00', '07:00']}}, 'schedule.phases.shop must be'),
        ({'shop': {'times': ['7am']}}, 'schedule.phases.shop must be'),
        ({'shop': {'times': ['07:00'], 'until': ['07:00']}}, 'schedule.phases.shop must be'),
        (
            {'shop': {'times': ['07:00', '15:00'], 'until': ['07:30']}},
            'schedule.phases.shop must be',
        ),
        ({'shop': {'times': ['07:00'], 'wake': 'yes'}}, 'schedule.phases.shop must be'),
        ({'shop': {'times': ['07:00']}, 'daily': {'times': ['08:00']}}, 'at least one phase'),
    ):
        problems = validate_manifest({**data, 'schedule': own_schedule(**bad)})
        assert any(fragment in problem for problem in problems), (bad, problems)


def test_own_times_become_slots_that_also_wake_on_their_own_server_times(tmp_path):
    project = project_for(tmp_path, schedule=own_schedule(shop={'times': ['07:00', '15:00']}))
    assert project.schedule.slot_map == {'shop': ('07:00', '15:00')}
    assert project.schedule.wake_phases == ('shop',)
    quiet = project_for(
        tmp_path / 'quiet', schedule=own_schedule(shop={'times': ['07:00'], 'wake': False})
    )
    assert quiet.schedule.wake_phases == ()


def test_each_server_time_wakes_the_phases_that_reported_it(tmp_path):
    from datetime import datetime

    project = project_for(tmp_path, schedule=own_schedule(shop={'times': ['07:00', '15:00']}))
    tz = project.schedule.tz

    def at(text):
        return datetime.fromisoformat(f'2026-11-17T{text}:00').replace(tzinfo=tz)

    class Ran:  # the 07:00 shop window already had a successful run
        def successful_starts(self, name, day, slots):
            return [('shop', '2026-11-17T07:00:30+08:00')]

    runner = RoundRunner(project, Publisher(), now=at('09:00'), metrics=Ran())
    assert runner.phases_for(at('12:00'), scheduled_at=at('12:00')) == ('daily',)  # fixed time
    assert runner.phases_for(at('15:00'), scheduled_at=at('15:00')) == ('shop',)  # its own slot
    wake = at('10:23').timestamp()
    runner.dynamic_candidates = (wake,)
    runner.dynamic_owners = {wake: frozenset({'shop'})}
    assert runner.phases_for(at('10:23'), scheduled_at=at('10:23')) == ('shop',)
    runner.dynamic_owners = {wake: frozenset({'*'})}
    assert runner.phases_for(at('10:23'), scheduled_at=at('10:23')) == ('daily',)
    runner.dynamic_owners = {wake: frozenset({'*', 'shop'})}
    assert runner.phases_for(at('10:23'), scheduled_at=at('10:23')) == ('daily', 'shop')
    runner.dynamic_owners = {}  # nobody known: the ordinary phases, as before
    assert runner.phases_for(at('10:23'), scheduled_at=at('10:23')) == ('daily',)


def test_a_phase_result_and_a_due_task_say_who_owns_a_server_time(tmp_path):
    from datetime import datetime, timedelta

    project = project_for(tmp_path, schedule=own_schedule(shop={'times': ['07:00', '15:00']}))
    now = datetime.fromisoformat('2026-11-17T09:00:00+08:00')
    view = project.new_projection(now, restore=False)
    soon, later, other = (now + timedelta(minutes=m) for m in (30, 90, 150))
    view.apply_result({'phase': 'shop', 'execution': 'waiting', 'finished_at': now.isoformat(),
                       'updates': [], 'dynamic_candidates': [soon.timestamp()]})  # fmt: skip
    view.apply_event({'kind': 'plan', 'plan': {
        'full_checklist_summary': {'goals': {
            'weekly_pack': {'status': 'waiting', 'next_run_at': later.isoformat()},
            'energy': {'status': 'waiting', 'next_run_at': other.isoformat()}}},
        'dynamic_candidates': [soon.timestamp(), later.timestamp(), other.timestamp()]}})  # fmt: skip
    view.apply_result({'phase': 'shop', 'execution': 'waiting', 'finished_at': now.isoformat(),
                       'updates': [], 'dynamic_candidates': [soon.timestamp()]})  # fmt: skip
    owners = view.dynamic_owners
    assert owners[soon.timestamp()] == {'shop'}
    assert owners[later.timestamp()] == {'shop'}  # 每周礼包 belongs to the shop phase
    assert owners[other.timestamp()] == {'*'}
    saved = project.sanitize(view.snapshot())
    assert saved['dynamic_owners'][later.isoformat(timespec='seconds')] == ['shop']


EVERY_TWO_HOURS = [f'{hour:02d}:00' for hour in range(6, 24, 2)]  # 06:00 … 22:00


def card_lines(card):
    import re

    out = []

    def walk(elements):
        for e in elements:
            if e['tag'] == 'collapsible_panel':
                out.append('[' + re.sub(r'<[^>]+>|\*\*', '', e['header']['title']['content']) + ']')
                walk(e['elements'])
            elif e['tag'] == 'markdown':
                out.append(re.sub(r'<[^>]+>|\*\*|~~', '', e['content']))
            elif e['tag'] == 'div':
                icon = e['icon'].get('token', e['icon']['tag'])
                out.append(icon + ' ' + re.sub(r'<[^>]+>|\*\*|~~', '', e['text']['content']))

    walk(card['body']['elements'])
    return out


def test_a_recurring_phase_heads_the_card_with_its_runs_and_next_time(tmp_path):
    project = project_for(tmp_path, schedule=own_schedule(shop={'times': EVERY_TWO_HOURS}))
    day = project.schedule.service_day('2026-11-17T11:00:00+08:00')
    snapshot = {
        'project': 'demo', 'service_day': day, 'discovered': True, 'phases': [],
        'observed_at': '2026-11-17T11:00:00+08:00', 'next_check_at': '2026-11-17T12:00:00+08:00',
        'slot_runs': {'shop': [0, 1]}, 'slot_fails': {'shop': [2]},
        'dynamic_candidates': ['2026-11-17T11:20:00+08:00'],
        'dynamic_owners': {'2026-11-17T11:20:00+08:00': ['shop']},
        'tasks': [
            {'task_id': 'weekly_pack', 'status': 'waiting', 'availability': 'open',
             'next_run_at': '2026-11-17T11:20:00+08:00'},
            {'task_id': 'signin', 'status': 'complete', 'availability': 'open',
             'done_at': '2026-11-17T06:01:00+08:00'},
            {'task_id': 'mail', 'status': 'incomplete', 'availability': 'open'},
        ],
    }  # fmt: skip
    card = project.render(project.sanitize(snapshot))
    lines = card_lines(card)
    plan = lines[0]
    assert '循环 · 商店：每 2 小时，下次 11:20，末次 22:00' in plan
    assert '每日：12:00 · 20:00末轮 · 05:00新一天' in plan  # the 11:20 time runs only 商店
    assert lines.index('循环任务') < lines.index('[商店　今日 2/9 次 · 下次 11:20]') < lines.index('每日任务')
    assert '今日　06:00✓  08:00✓  10:00✕' in lines
    assert 'time_outlined 每周礼包　等待 · 11:20执行' in lines
    assert 'ellipse_outlined 邮件奖励　12:00执行' in lines  # not at 商店's own 11:20
    assert 'yes_outlined 每日签到　06:01' in lines
    assert card['header']['subtitle']['content'] == '每日 1/2 · 循环 1 项'


def test_a_later_phases_plan_keeps_the_wakes_earlier_phases_reported(tmp_path):
    from datetime import datetime, timedelta

    project = project_for(tmp_path, schedule=own_schedule(shop={'times': ['07:00', '15:00']}))
    now = datetime.fromisoformat('2026-11-17T09:00:00+08:00')
    view = project.new_projection(now, restore=False)
    a, b, c = (now + timedelta(minutes=m) for m in (30, 90, 150))

    def plan(phase, *wakes):
        view.apply_event({'kind': 'plan', 'phase': phase, 'plan': {
            'full_checklist_summary': {'goals': {}},
            'dynamic_candidates': [wake.timestamp() for wake in wakes]}})  # fmt: skip

    plan('shop', a)
    view.apply_result({'phase': 'shop', 'execution': 'waiting', 'finished_at': now.isoformat(),
                       'updates': [], 'dynamic_candidates': [b.timestamp()]})  # fmt: skip
    plan('daily')  # the ordinary phase's plan reports no server time
    assert view.dynamic_candidates == (a.timestamp(), b.timestamp())
    plan('shop', c)  # the shop phase's newer plan replaces its own earlier one
    assert view.dynamic_candidates == (b.timestamp(), c.timestamp())
    # A plan claims no owner, so a later phase repeating the shop's time does
    # not wake the ordinary phases too.
    plan('daily', b)
    assert view.dynamic_owners == {b.timestamp(): {'shop'}}


def test_a_read_only_discovery_claims_no_server_time_itself(tmp_path):
    from datetime import datetime, timedelta

    project = project_for(tmp_path, schedule=own_schedule(shop={'times': ['07:00', '15:00']}))
    assert project.status_phase in project.catalog.phases
    now = datetime.fromisoformat('2026-11-17T09:00:00+08:00')
    view = project.new_projection(now, restore=False)
    later = now + timedelta(minutes=90)
    # What observe() emits after a discovery run: the status phase's plan.
    view.apply_event({'kind': 'plan', 'phase': project.status_phase, 'plan': {
        'full_checklist_summary': {'goals': {
            'weekly_pack': {'status': 'waiting', 'next_run_at': later.isoformat()}}},
        'dynamic_candidates': [later.timestamp()]}})  # fmt: skip
    assert view.dynamic_owners == {later.timestamp(): {'shop'}}  # 每周礼包 wakes only the shop


def test_a_time_limited_phase_is_not_caught_up_after_its_deadline(tmp_path):
    from datetime import datetime

    from autocontrol.runner import RoundRunner

    shop = {'times': ['07:00'], 'until': ['07:20']}
    project = project_for(tmp_path, schedule=own_schedule(shop=shop))
    tz = project.schedule.tz

    def at(text):
        return datetime.fromisoformat(f'2026-11-17T{text}:00').replace(tzinfo=tz)

    runner = RoundRunner(project, Publisher(), now=at('07:00'))
    assert runner.phases_for(at('07:10'), scheduled_at=None) == ('daily', 'shop')  # retry in time
    assert runner.phases_for(at('12:00'), scheduled_at=at('12:00')) == ('daily',)  # missed
    view = project.new_projection(at('12:00'), restore=False)
    view.mark_slot('shop', at('07:00'), failed=True)
    seen = {**view.snapshot(), 'observed_at': at('12:00').isoformat(timespec='seconds')}
    card = json.dumps(project.render(seen), ensure_ascii=False)
    assert '07:00' in card and '✕' in card  # the missed slot is a cross ...
    assert '下次 明日07:00' in card  # ... and the next run is tomorrow's slot


def test_done_text_fills_only_declared_counts_around_a_few_words():
    data = json.loads(DEMO_MANIFEST.read_text())
    data['tasks'][0]['done_text'] = '已领 {claimed} 次'
    assert validate_manifest(data) == []
    for bad, fragment in (
        ('推进 {cleared} 关', 'undeclared count_keys: cleared'),
        ('今日已完成', 'done_text must be up to 24 characters'),  # no field
        ('{claimed}' * 5, 'done_text must be up to 24 characters'),
        ('已领 {claimed} 次 {', 'done_text must be up to 24 characters'),
        ('很' * 25 + '{claimed}', 'done_text must be up to 24 characters'),
        ('**{claimed}**', 'done_text must be up to 24 characters'),
        (3, 'done_text must be a string'),
    ):
        data['tasks'][0]['done_text'] = bad
        assert any(fragment in problem for problem in validate_manifest(data)), bad


def test_a_finished_row_shows_its_manifest_text_filled_from_the_reported_counts(tmp_path):
    tasks = [
        {'id': 'story', 'title': '主线推进', 'section': 'daily',
         'done_text': '推进 {cleared} 关 · 当前 {chapter}-{stage}'},
        {'id': 'signin', 'title': '每日签到', 'section': 'daily', 'done_text': '连签 {streak} 天'},
        {'id': 'mail', 'title': '邮件奖励', 'section': 'daily', 'done_text': '已领 {cleared} 封'},
    ]  # fmt: skip
    project = project_for(
        tmp_path, tasks=tasks, count_keys=['cleared', 'chapter', 'stage', 'streak']
    )
    assert project.catalog.tasks['story'].done_text == '推进 {cleared} 关 · 当前 {chapter}-{stage}'
    day = project.schedule.service_day('2026-11-17T13:00:00+08:00')
    snapshot = {
        'project': 'demo', 'service_day': day, 'discovered': True, 'phases': [],
        'observed_at': '2026-11-17T13:00:00+08:00', 'next_check_at': '2026-11-17T20:00:00+08:00',
        'tasks': [
            {'task_id': 'story', 'status': 'complete', 'availability': 'open',
             'done_at': '2026-11-17T12:10:00+08:00',
             'counts': {'cleared': 3, 'chapter': 20, 'stage': 1}},
            {'task_id': 'signin', 'status': 'complete', 'availability': 'open',
             'done_at': '2026-11-17T06:01:00+08:00', 'counts': {'cleared': 1}},
            {'task_id': 'mail', 'status': 'incomplete', 'availability': 'open',
             'counts': {'cleared': 2}},
        ],
    }  # fmt: skip
    lines = card_lines(project.render(project.sanitize(snapshot)))
    assert 'yes_outlined 主线推进　12:10 · 推进 3 关 · 当前 20-1' in lines
    assert 'yes_outlined 每日签到　06:01' in lines  # a field it names was not reported
    assert not any('已领' in line for line in lines)  # only a finished row shows its text


def test_a_task_may_name_the_phase_that_runs_it():
    data = json.loads(DEMO_MANIFEST.read_text())
    data['tasks'][0]['phase'] = 'shop'
    assert validate_manifest(data) == []
    for index, phase in ((0, 'status'), (0, 'ghost'), (2, 'shop')):  # observe, unknown, a step
        bad = json.loads(DEMO_MANIFEST.read_text())
        bad['tasks'][index]['phase'] = phase
        problems = validate_manifest(bad)
        assert any('phase must be one of phases' in p for p in problems), (index, phase)


def test_a_task_run_by_a_recurring_phase_shows_that_phases_next_time(tmp_path):
    tasks = [
        {'id': 'signin', 'title': '每日签到', 'section': 'daily', 'phase': 'shop'},
        {'id': 'mail', 'title': '邮件奖励', 'section': 'daily'},
        {'id': 'weekly_pack', 'title': '每周礼包', 'section': 'shop', 'kind': 'job'},
    ]
    project = project_for(
        tmp_path, tasks=tasks, schedule=own_schedule(shop={'times': EVERY_TWO_HOURS})
    )
    catalog = project.catalog
    assert catalog.tasks['signin'].phase == 'shop'
    assert (catalog.run_section('signin'), catalog.run_section('mail')) == ('shop', 'daily')
    day = project.schedule.service_day('2026-11-17T12:30:00+08:00')
    snapshot = {
        'project': 'demo', 'service_day': day, 'discovered': True, 'phases': [],
        'observed_at': '2026-11-17T12:30:00+08:00', 'next_check_at': '2026-11-17T14:00:00+08:00',
        'tasks': [
            {'task_id': 'signin', 'status': 'incomplete', 'availability': 'open',
             'current': 60, 'total': 100},
            {'task_id': 'mail', 'status': 'incomplete', 'availability': 'open'},
            {'task_id': 'weekly_pack', 'status': 'waiting', 'availability': 'open'},
        ],
    }  # fmt: skip
    lines = card_lines(project.render(project.sanitize(snapshot)))
    # Listed and judged as a daily task, run at the shop phase's 14:00 slot.
    assert 'ellipse_outlined 每日签到　60/100 · 14:00执行' in lines
    assert 'ellipse_outlined 邮件奖励　20:00执行' in lines  # the next ordinary round


def test_a_due_task_run_by_a_wake_phase_gives_that_phase_its_time(tmp_path):
    from datetime import datetime, timedelta

    tasks = json.loads(DEMO_MANIFEST.read_text())['tasks']
    next(task for task in tasks if task['id'] == 'energy')['phase'] = 'shop'
    schedule = own_schedule(shop={'times': ['07:00', '15:00']})
    project = project_for(tmp_path, tasks=tasks, schedule=schedule)
    now = datetime.fromisoformat('2026-11-17T09:00:00+08:00')
    view = project.new_projection(now, restore=False)
    later = now + timedelta(minutes=150)
    goals = {'energy': {'status': 'waiting', 'next_run_at': later.isoformat()}}
    view.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': goals},
                                               'dynamic_candidates': [later.timestamp()]}})  # fmt: skip
    assert view.dynamic_owners[later.timestamp()] == {'shop'}  # not '*': shop runs 体力领取


IDLE = {'reason': 'insufficient_materials', 'times': ['06:00', '14:00', '22:00']}


def test_a_phase_may_drop_to_idle_times_on_a_reason():
    data = json.loads(DEMO_MANIFEST.read_text())
    good = own_schedule(shop={'times': EVERY_TWO_HOURS, 'idle': IDLE})
    assert validate_manifest({**data, 'schedule': good}) == []
    for idle, fragment in (
        ({'reason': 'insufficient_materials'}, 'schedule.phases.shop must be'),
        ({'reason': 'insufficient_materials', 'times': []}, 'schedule.phases.shop must be'),
        ({'reason': 'Bad', 'times': ['06:00']}, 'schedule.phases.shop must be'),
        ({'reason': 'made_up_reason', 'times': ['06:00']}, 'idle.reason must be a known'),
    ):
        schedule = own_schedule(shop={'times': ['06:00'], 'idle': idle})
        problems = validate_manifest({**data, 'schedule': schedule})
        assert any(fragment in problem for problem in problems), (idle, problems)


def test_a_phase_out_of_work_runs_and_counts_only_its_idle_times(tmp_path):
    from datetime import datetime

    schedule = own_schedule(shop={'times': EVERY_TWO_HOURS, 'idle': IDLE})
    project = project_for(tmp_path, schedule=schedule)
    assert project.schedule.idle == (
        ('shop', 'insufficient_materials', ('06:00', '14:00', '22:00')),
    )
    tz = project.schedule.tz

    def at(text):
        return datetime.fromisoformat(f'2026-11-17T{text}:00').replace(tzinfo=tz)

    view = project.new_projection(at('11:00'), restore=False)
    for text in ('06:00', '08:00', '10:00'):
        view.mark_slot('shop', at(text))
    assert view.slot_runs == {'shop': frozenset({0, 1, 2})}  # three of the nine windows

    def report(status, reason=None):
        source = {'status': status, **({'reason': reason} if reason else {})}
        goals = {'weekly_pack': source}
        view.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': goals}}})

    report('blocked', 'insufficient_materials')
    assert view.effective_schedule.slot_map['shop'] == ('06:00', '14:00', '22:00')
    assert view.slot_runs == {'shop': frozenset({0})}  # all of them fall in the 06:00 window
    runner = RoundRunner(project, Publisher(), now=at('11:00'))
    runner._slot_view = lambda now: view
    assert runner.phases_for(at('12:00'), scheduled_at=at('12:00')) == ('daily',)  # not its time
    assert runner.phases_for(at('14:00'), scheduled_at=at('14:00')) == ('shop',)
    assert runner.effective_schedule(at('12:30')).next_wake(at('12:30'))[0] == at('14:00')
    shown = {**view.snapshot(), 'observed_at': at('11:00').isoformat()}  # the card's clock
    lines = card_lines(project.render(project.sanitize(shown)))
    assert '循环 · 商店：06:00 · 14:00 · 22:00，下次 14:00' in lines[0]
    assert '[商店　今日 1/3 次 · 下次 14:00]' in lines
    assert 'time_outlined 每周礼包　材料不足 · 14:00执行' in lines
    # A restart restores the idle windows that were saved (exact times come back
    # from the metrics; the saved card alone knows only the window).
    from autocontrol.board.projection import Projection

    saved = project.sanitize(view.snapshot())
    again = Projection(project.catalog, project.schedule, at('11:30'), saved)
    assert saved['slot_runs'] == {'shop': [0]} and again.slot_runs == {'shop': frozenset({0})}
    report('waiting')  # materials are back: all nine times again, no run forgotten
    assert view.slot_runs == {'shop': frozenset({0, 1, 2})}
    assert runner.phases_for(at('12:00'), scheduled_at=at('12:00')) == ('daily', 'shop')


def test_an_unfinished_row_shows_its_manifest_progress_text(tmp_path):
    words = '已推 {cleared} 关 · 当前 {chapter}-{stage}'
    tasks = [
        {'id': 'story', 'title': '主线推进', 'section': 'daily', 'progress_text': words},
        {'id': 'signin', 'title': '每日签到', 'section': 'daily', 'progress_text': words},
        {'id': 'mail', 'title': '邮件奖励', 'section': 'daily', 'progress_text': '已领 {cleared} 封'},
        {'id': 'energy', 'title': '体力领取', 'section': 'daily', 'progress_text': '已领 {cleared} 次'},
    ]
    keys = ['cleared', 'chapter', 'stage']
    project = project_for(tmp_path, tasks=tasks, count_keys=keys)
    day = project.schedule.service_day('2026-11-17T13:00:00+08:00')
    counts = {'cleared': 3, 'chapter': 20, 'stage': 1}
    snapshot = {
        'project': 'demo', 'service_day': day, 'discovered': True, 'phases': [],
        'observed_at': '2026-11-17T13:00:00+08:00', 'next_check_at': '2026-11-17T20:00:00+08:00',
        'tasks': [
            {'task_id': 'story', 'status': 'blocked', 'reason': 'insufficient_stamina',
             'availability': 'open', 'counts': counts},
            {'task_id': 'signin', 'status': 'blocked', 'reason': 'insufficient_stamina',
             'availability': 'open', 'counts': {'cleared': 3}},
            {'task_id': 'mail', 'status': 'running', 'availability': 'open', 'counts': {'cleared': 2}},
            {'task_id': 'energy', 'status': 'failed', 'availability': 'open', 'counts': {'cleared': 1}},
        ],
    }  # fmt: skip
    lines = card_lines(project.render(project.sanitize(snapshot)))
    assert any(line.startswith('ellipse_outlined 主线推进　已推 3 关 · 当前 20-1 · ') for line in lines)
    assert any(line.startswith('ellipse_outlined 每日签到　体力不足') for line in lines)  # a count missing
    assert any(line.startswith('loading_outlined 邮件奖励　执行中 · 已领 2 封') for line in lines)
    assert any(line.startswith('warning_outlined 体力领取　失败') for line in lines)  # a failure wins
    data = json.loads(DEMO_MANIFEST.read_text())
    data['tasks'][0]['progress_text'] = '已推 {ghost} 关'
    assert any(
        'progress_text uses undeclared count_keys: ghost' in p for p in validate_manifest(data)
    )


def test_a_time_that_passed_while_a_later_phase_ran_is_overdue_for_its_owner(tmp_path):
    from datetime import datetime, timedelta

    project = project_for(tmp_path, schedule=own_schedule(shop={'times': ['07:00', '15:00']}))
    start = datetime.fromisoformat('2026-11-17T15:00:00+08:00')
    view = project.new_projection(start, restore=False)
    retry, ended = start + timedelta(minutes=3), start + timedelta(minutes=5)
    view.apply_result({'phase': 'shop', 'execution': 'waiting', 'finished_at': start.isoformat(),
                       'updates': [], 'dynamic_candidates': [retry.timestamp()]})  # fmt: skip
    assert view.overdue_wakes() == {}  # still ahead
    # A time only a plan reported is never caught up.
    view.apply_event({'kind': 'plan', 'phase': 'daily', 'plan': {
        'full_checklist_summary': {'goals': {}},
        'dynamic_candidates': [(start + timedelta(minutes=1)).timestamp()]}})  # fmt: skip
    soon, later = ended + timedelta(seconds=10), ended + timedelta(minutes=1)
    view.apply_result({'phase': 'daily', 'execution': 'complete', 'finished_at': ended.isoformat(),
                       'updates': [], 'dynamic_candidates': [soon.timestamp(), later.timestamp()]})  # fmt: skip
    view.finish_run(ended + timedelta(seconds=20))
    # The shop's retry passed while the daily phase ran. The daily phase's own
    # times came after its result; one of them passed while the card was sent.
    assert view.overdue_wakes() == {retry.timestamp(): frozenset({'shop'})}
    assert view.dynamic_candidates == (later.timestamp(),)


def test_the_runner_keeps_a_passed_time_for_its_owner_until_the_service_takes_it(tmp_path):
    from datetime import datetime, timedelta
    from unittest.mock import patch

    project = project_for(tmp_path, schedule=own_schedule(shop={'times': ['07:00', '15:00']}))
    start = datetime.fromisoformat('2026-11-17T15:00:00').replace(tzinfo=project.schedule.tz)
    retry = start + timedelta(minutes=3)
    clock = {'now': start}

    def run_phase(name, day, **kwargs):
        began = clock['now']
        clock['now'] += timedelta(minutes=1 if name == 'shop' else 4)
        return {'command': name, 'game_day': day, 'exit_code': 0, 'tasks': {},
                'execution': 'waiting' if name == 'shop' else 'complete',
                'protocol': {'result_valid': True},
                'started_at': began.isoformat(), 'finished_at': clock['now'].isoformat(),
                'next_wake': [retry.timestamp()] if name == 'shop' else []}  # fmt: skip

    runner = RoundRunner(project, Publisher(), now=start)
    with (
        patch.object(project, 'run_phase', side_effect=run_phase),
        patch.object(type(project.schedule), 'now', lambda self: clock['now']),
    ):
        runner.run_once(now=start, phases=('shop', 'daily'))
        assert runner.dynamic_owners[retry.timestamp()] == {'shop'}
        # Only the shop phase that reported the time runs for it.
        assert runner.phases_for(clock['now'], scheduled_at=retry) == ('shop',)
    assert runner.take_overdue() == (retry.timestamp(),)
    assert runner.take_overdue() == ()
