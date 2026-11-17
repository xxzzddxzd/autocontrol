"""yslzm adapter: record translation and the real child protocol end to end."""

import json
import sys
import textwrap

from autocontrol.projects import load
from autocontrol.runner import RoundRunner

FAKE_MAIN = textwrap.dedent(
    '''
    import json, os, sys, time
    phase, day = sys.argv[1], sys.argv[3]
    fd = int(os.environ['YSLZM_PROGRESS_FD'])
    def emit(event):
        os.write(fd, json.dumps({**event, 'phase': phase, 'observed_at': time.time()}).encode() + b'\\n')
    if '--invocation-scope' in sys.argv:
        json.loads(sys.stdin.read())
    emit({'kind': 'plan', 'plan': {'game_day': day, 'full_checklist_summary': {'goals': {
        'arena': {'status': 'complete', 'counts': {'obtained': 12, 'limit': 12}}}}}})
    summary = {'game_day': day}
    if phase == 'status':
        summary['execution'] = 'complete'
    elif phase == 'cardbook':
        emit({'kind': 'task_started', 'task_id': 'cardbook'})
        emit({'kind': 'task_progress', 'task_id': 'cardbook', 'current': 5, 'total': 13, 'unit': '派遣'})
        summary.update(kind='cardbook_daily', status='awaiting_completion', ordinary_sent=5,
                       ordinary_limit=13, running_tasks=5)
    elif phase == 'auto':
        emit({'kind': 'task_failed', 'task_id': 'pet_care', 'reason': 'action_exception'})
        summary.update(execution='partial', full_checklist_summary={'scope': 'full_daily', 'goals': {
            'pets': {'status': 'incomplete', 'attempted': True}}})
    else:
        summary.update(execution='partial', policy_jobs={})
    print('private noise that must not be kept')
    print(json.dumps(summary))
    '''
)


class Publisher:
    status = {'status': 'sent'}

    def __init__(self):
        self.snapshots = []

    def submit(self, snapshot):
        self.snapshots.append(snapshot)

    def flush(self, timeout):
        return True


def test_registry_and_defaults(tmp_path):
    project = load('yslzm', workdir=tmp_path, private=tmp_path)
    assert project.phases == ('cardbook', 'auto', 'craft', 'wardrobe', 'activities')
    assert project.uuid_namespace == 'yslzm-daily-board'
    assert project.round_lock == tmp_path / 'round.lock'


def test_result_facts_keep_only_this_phases_jobs_and_cardbook_status(project):
    facts = project.result_facts(
        {
            'command': 'craft',
            'game_day': '2026-11-17',
            'execution': 'partial',
            'policy_jobs': {
                'gold_purchase': {'status': 'complete'},
                'qianhuan': {'status': 'complete'},
                'unknown_job': {'status': 'complete'},
            },
        }
    )
    assert facts['updates'] == [('gold_purchase', {'status': 'complete'})]
    book = project.result_facts(
        {
            'command': 'cardbook',
            'cardbook_status': 'awaiting_completion',
            'failure_stage': 'summary',
            'ordinary_sent': 5,
            'execution': 'partial',
        }
    )
    assert [key for key, _ in book['updates']] == ['cardbook', 'cardbook']
    assert book['updates'][1][1] == {
        'status': 'result_unavailable',
        'reason': 'ended_without_result',
    }
    assert book['reason'] == 'summary'


def test_full_round_with_a_real_child_updates_one_card(project, private):
    (project.workdir / 'main.py').write_text(FAKE_MAIN)
    publisher = Publisher()
    runner = RoundRunner(project, publisher, now='2026-11-17T09:20:00+08:00')
    result = runner.run_once(now=project.schedule.local('2026-11-17T09:20:00+08:00'))
    assert result['status'] == 'finished'
    final = publisher.snapshots[-1]
    rows = {row['task_id']: row for row in final['tasks']}
    assert rows['arena']['status'] == 'complete'
    assert rows['pets']['status'] == 'failed' and rows['pets']['reason'] == 'action_exception'
    assert rows['cardbook']['status'] == 'waiting'
    assert (rows['cardbook']['current'], rows['cardbook']['total']) == (5, 13)
    assert {row['phase'] for row in final['phases']} == set(project.phases)
    card = json.dumps(project.render(final), ensure_ascii=False)
    assert '竞技场' in card and '执行时发生异常，未收到成功结果' in card
    saved = sorted((private / 'results').glob('*/*.json'))
    assert len(saved) == len(project.phases)
    assert 'private noise' not in ''.join(path.read_text() for path in saved)


def test_read_only_discovery_with_a_real_child(project):
    (project.workdir / 'main.py').write_text(FAKE_MAIN)
    publisher = Publisher()
    result = RoundRunner(project, publisher).run_once(
        observe_only=True, now=project.schedule.local('2026-11-17T05:01:00+08:00')
    )
    assert (result['status'], result['code']) == ('observed', 0)
    final = publisher.snapshots[-1]
    assert next(row for row in final['tasks'] if row['task_id'] == 'arena')['status'] == 'complete'
    assert final['phases'] == [{'phase': 'status', 'title': '计划读取', 'status': 'complete'}]
    assert sys.executable  # the worker ran with this interpreter
