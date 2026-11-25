"""Round runner (ported from yslzm/tests/test_daily_board_controller.py, plus run-policy cases)."""

from datetime import datetime
import json
import os
import subprocess
from unittest.mock import patch

from autocontrol import locks
from autocontrol.lark.store import BoardStore
from autocontrol.runner import RoundRunner

NOW = datetime.fromisoformat('2026-11-17T09:00:00+08:00')
PHASES = ('cardbook', 'auto', 'craft', 'wardrobe', 'activities')


class Publisher:
    status = {'status': 'sent'}

    def __init__(self, private):
        self.private = private
        self.snapshots = []
        self.flushes = 0

    def submit(self, snapshot):
        self.snapshots.append(snapshot)
        return True

    def flush(self, timeout):
        self.flushes += 1
        # The plan is delivered before a game session exists; the final delivery
        # happens after even the round lock was released.
        lock = 'state/runtime.lock' if self.flushes == 1 else 'round.lock'
        os.close(locks.acquire(self.private / lock))
        return True


def runner(project, publisher, now=NOW):
    return RoundRunner(project, publisher, now=now)


def tasks(snapshot):
    return {row['task_id']: row for row in snapshot['tasks']}


def test_one_failure_neither_skips_nor_replays_independent_phases(project, private):
    publisher, called = Publisher(private), []

    def run(phase, day, **kwargs):
        called.append(phase)
        kwargs['on_event'](
            {
                'kind': 'plan',
                'phase': phase,
                'plan': {
                    'game_day': day,
                    'full_checklist_summary': {
                        'goals': {'cardbook': {'status': 'waiting', 'availability': 'open'}}
                    },
                },
            }
        )
        return {
            'command': phase,
            'game_day': day,
            'execution': 'failed' if phase == 'auto' else 'partial',
            'exit_code': 1 if phase == 'auto' else 0,
        }, None

    with patch('autocontrol.projects.yslzm.run_scheduled', side_effect=run):
        result = runner(project, publisher).run_once()
    assert called == list(PHASES)
    assert result['code'] == 1 and result['status'] == 'finished'
    assert result['delivery']['status'] == 'sent'


def test_unconfirmed_phases_are_never_replayed(project, private):
    for state in ('pending_reconciliation', 'uncertain', 'session_refresh_required'):
        called = []

        def run(phase, day, called=called, state=state, **kwargs):
            called.append(phase)
            return {'command': phase, 'execution': state, 'exit_code': 0}, None

        with patch('autocontrol.projects.yslzm.run_scheduled', side_effect=run):
            result = runner(project, Publisher(private)).run_once()
        assert called == list(PHASES), state
        assert result['code'] == 1


def test_plan_is_delivered_before_the_worker_reads_the_server(project, private):
    publisher, order = Publisher(private), []
    original = publisher.flush

    def flush(timeout):
        order.append('card_delivered')
        return original(timeout)

    publisher.flush = flush

    def worker(argv, **kwargs):
        order.append('worker_started')
        before = tasks(publisher.snapshots[0])
        assert before['arena']['status'] == 'not_started'
        kwargs['on_event'](
            {
                'kind': 'plan',
                'phase': 'status',
                'plan': {
                    'game_day': '2026-11-17',
                    'full_checklist_summary': {
                        'goals': {
                            'arena': {'status': 'complete', 'counts': {'obtained': 12, 'limit': 12}}
                        }
                    },
                },
            }
        )
        return subprocess.CompletedProcess(argv, 0, stdout=b'{}\n')

    with patch('autocontrol.projects.yslzm.run_streamed', side_effect=worker):
        result = runner(project, publisher).run_once(
            observe_only=True, now=datetime.fromisoformat('2026-11-17T10:00:00+08:00')
        )
    assert order[:2] == ['card_delivered', 'worker_started']
    assert tasks(publisher.snapshots[-1])['arena']['status'] == 'complete'
    assert {s['service_day'] for s in publisher.snapshots} == {'2026-11-17'}
    assert result['status'] == 'observed' and result['code'] == 0


def test_failed_initial_notification_cannot_block_execution(project, private):
    publisher, called = Publisher(private), []

    def broken(timeout):
        raise RuntimeError('notification transport unavailable')

    publisher.flush = broken

    def run(phase, day, **kwargs):
        called.append(phase)
        return {'command': phase, 'game_day': day, 'execution': 'partial', 'exit_code': 0}, None

    with patch('autocontrol.projects.yslzm.run_scheduled', side_effect=run):
        result = runner(project, publisher).run_once()
    assert called == list(PHASES)
    assert result['initial_delivery']['status'] == 'failed'


def test_saved_success_restores_display_but_never_skips_business(project, private):
    day = project.schedule.service_day()
    BoardStore(project.board_db, sanitize=project.sanitize).save(
        'yslzm',
        day,
        'oc_test',
        snapshot={
            'project': 'yslzm',
            'service_day': day,
            'tasks': [
                {
                    'task_id': 'palette_unlock',
                    'phase': 'wardrobe',
                    'status': 'complete',
                    'availability': 'open',
                }
            ],
        },
    )
    called = []

    def run(phase, day, **kwargs):
        called.append(phase)
        return {'command': phase, 'game_day': day, 'execution': 'partial', 'exit_code': 0}, None

    round_runner = RoundRunner(project, Publisher(private))
    with patch('autocontrol.projects.yslzm.run_scheduled', side_effect=run):
        round_runner.run_once()
    assert called == list(PHASES)
    assert tasks(round_runner.projection.snapshot())['palette_unlock']['status'] == 'complete'


def test_busy_account_never_opens_another_connection(project, private):
    publisher = Publisher(private)
    fd = locks.acquire(private / 'state/runtime.lock')
    try:
        with patch('autocontrol.projects.yslzm.run_streamed') as stream:
            result = runner(project, publisher).run_once(observe_only=True)
        stream.assert_not_called()
        assert result['status'] == 'busy' and publisher.snapshots == []
    finally:
        os.close(fd)


def test_account_taken_by_a_manual_command_stops_the_round(project, private):
    called = []

    def run(phase, day, **kwargs):
        called.append(phase)
        return {
            'command': phase,
            'execution': 'already_running',
            'exit_code': 2,
            'failure_stage': 'runtime_lock',
        }, None

    with patch('autocontrol.projects.yslzm.run_scheduled', side_effect=run):
        result = runner(project, Publisher(private)).run_once()
    assert called == ['cardbook'] and result['code'] == 2


def test_a_crashing_phase_does_not_stop_the_round(project, private):
    called = []

    def run(phase, day, **kwargs):
        called.append(phase)
        if phase == 'craft':
            raise RuntimeError('unexpected')
        return {'command': phase, 'game_day': day, 'execution': 'complete', 'exit_code': 0}, None

    publisher = Publisher(private)
    with patch('autocontrol.projects.yslzm.run_scheduled', side_effect=run):
        result = runner(project, publisher).run_once()
    assert called == list(PHASES) and result['code'] == 1
    phases = {row['phase']: row for row in publisher.snapshots[-1]['phases']}
    assert phases['craft']['status'] == 'failed' and phases['activities']['status'] == 'complete'
    assert not publisher.snapshots[-1]['running']


def test_cross_day_finish_updates_the_original_card(project, private):
    round_runner = runner(
        project, Publisher(private), datetime.fromisoformat('2026-11-17T04:50:00+08:00')
    )
    event = round_runner._event
    event(
        {
            'kind': 'plan',
            'phase': 'cardbook',
            'plan': {'game_day': '2026-11-16', 'full_checklist_summary': {'goals': {}}},
        }
    )
    event(
        {
            'kind': 'task_started',
            'phase': 'cardbook',
            'task_id': 'cardbook',
            'game_day': '2026-11-16',
        }
    )
    event(
        {
            'kind': 'plan',
            'phase': 'cardbook',
            'plan': {'game_day': '2026-11-17', 'full_checklist_summary': {'goals': {}}},
        }
    )
    event(
        {
            'kind': 'task_finished',
            'phase': 'cardbook',
            'task_id': 'cardbook',
            'status': 'complete',
            'game_day': '2026-11-16',
        }
    )
    assert tasks(round_runner._views['2026-11-16'].snapshot())['cardbook']['status'] == 'complete'
    assert (
        tasks(round_runner._views['2026-11-17'].snapshot())['cardbook']['status'] == 'not_started'
    )
    assert round_runner.projection.day == '2026-11-17'


def test_failed_discovery_is_visible_and_is_not_a_business_failure(project, private):
    publisher = Publisher(private)
    result = subprocess.CompletedProcess(
        [], 1, stdout=b'{"execution":"failed","failure_stage":"connect"}\n'
    )
    with patch('autocontrol.projects.yslzm.run_streamed', return_value=result):
        outcome = runner(project, publisher).run_once(observe_only=True)
    assert outcome['code'] == 1
    phase = publisher.snapshots[-1]['phases'][-1]
    assert (phase['phase'], phase['status'], phase['reason']) == ('status', 'failed', 'connect')


def test_unknown_targets_pass_between_phases_only_in_memory(project, private):
    inputs = []

    def child(argv, **kwargs):
        inputs.append(json.loads(kwargs['input']))
        target = ['shop', len(inputs)]
        summary = {
            'game_day': '2026-11-17',
            'execution': 'partial',
            'run_scope': {'uncertain_targets': [target]},
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(summary).encode() + b'\n')

    with patch('autocontrol.projects.yslzm.phases.run_streamed', side_effect=child):
        runner(project, Publisher(private)).run_once()
    assert inputs[0] == {'uncertain_targets': []}
    assert inputs[-1] == {'uncertain_targets': [['shop', n] for n in range(1, len(PHASES))]}
    saved = ''.join(path.read_text() for path in (private / 'results').glob('*/*.json'))
    assert saved and 'uncertain_targets' not in saved


# -- shared logins: one worker process per round --------------------------------


def _shared(private):
    (private / 'autocontrol.json').write_text(json.dumps({'shared_session': True}))


def _record(phase, day, **extra):
    return {'command': phase, 'game_day': day, 'execution': 'partial', 'exit_code': 0, **extra}


def _single(called):
    def run(phase, day, **kwargs):
        called.append(phase)
        return _record(phase, day), None

    return run


def test_shared_round_finishes_each_phase_as_the_worker_reports_it(project, private):
    _shared(private)
    publisher, rounds, single = Publisher(private), [], []

    def run_round(phases, day, *, on_phase, **kwargs):
        rounds.append(phases)
        for phase in phases:
            before = len(publisher.snapshots)
            on_phase(phase, _record(phase, day))
            assert len(publisher.snapshots) > before  # the card updates per phase
        return phases

    with patch('autocontrol.projects.yslzm.run_round', side_effect=run_round), patch(
        'autocontrol.projects.yslzm.run_scheduled', side_effect=_single(single)
    ):
        result = runner(project, publisher).run_once()
    assert rounds == [PHASES] and single == []
    assert result['code'] == 0 and result['status'] == 'finished' and result['phases'] == 5


def test_phases_the_shared_worker_never_reached_run_on_their_own(project, private):
    _shared(private)
    single = []

    def run_round(phases, day, *, on_phase, **kwargs):
        for phase in phases[:2]:
            on_phase(phase, _record(phase, day))
        return phases[:2]

    with patch('autocontrol.projects.yslzm.run_round', side_effect=run_round), patch(
        'autocontrol.projects.yslzm.run_scheduled', side_effect=_single(single)
    ):
        result = runner(project, Publisher(private)).run_once()
    assert single == ['craft', 'wardrobe', 'activities']
    assert result['phases'] == 5


def test_a_shared_worker_error_reports_the_phase_in_flight_and_never_reruns_it(project, private):
    _shared(private)
    single = []

    def run_round(phases, day, *, on_phase, **kwargs):
        on_phase('cardbook', _record('cardbook', day))
        raise RuntimeError('synthetic adapter failure')

    with patch('autocontrol.projects.yslzm.run_round', side_effect=run_round), patch(
        'autocontrol.projects.yslzm.run_scheduled', side_effect=_single(single)
    ):
        result = runner(project, Publisher(private)).run_once()
    assert single == ['craft', 'wardrobe', 'activities']
    assert result['code'] == 1 and result['phases'] == 5


def test_a_busy_account_stops_the_shared_round(project, private):
    _shared(private)
    single = []

    def run_round(phases, day, *, on_phase, **kwargs):
        for phase in phases:
            on_phase(phase, {'command': phase, 'execution': 'already_running', 'exit_code': 2})
        return phases

    with patch('autocontrol.projects.yslzm.run_round', side_effect=run_round), patch(
        'autocontrol.projects.yslzm.run_scheduled', side_effect=_single(single)
    ):
        result = runner(project, Publisher(private)).run_once()
    assert single == [] and result['phases'] == 1


def test_a_single_phase_command_never_uses_the_shared_worker(project, private):
    _shared(private)
    single = []
    with patch('autocontrol.projects.yslzm.run_round', side_effect=AssertionError), patch(
        'autocontrol.projects.yslzm.run_scheduled', side_effect=_single(single)
    ):
        runner(project, Publisher(private)).run_once(phase='auto')
    assert single == ['auto']


# -- slotted phases: 事件簿 runs at 06:00, 14:00 and 22:00 -------------------------


class SlotMetrics:
    def __init__(self, starts=()):
        self.starts = list(starts)

    def successful_starts(self, project, day, phases):
        return [(phase, started) for phase, started in self.starts if phase in phases]

    def __getattr__(self, name):  # record_* calls are accepted and ignored
        return lambda *args, **kwargs: None


def _at(text):
    return datetime.fromisoformat(f'2026-11-17T{text}:00+08:00')


def test_each_wake_runs_the_phases_due_at_it(project, private):
    rest = ('auto', 'craft', 'wardrobe', 'activities')
    behind = RoundRunner(project, Publisher(private), now=NOW, metrics=SlotMetrics())
    assert behind.phases_for(_at('10:00')) == PHASES  # start-up: the 06:00 window still lacks a run
    assert behind.phases_for(_at('13:15'), scheduled_at=_at('13:15')) == PHASES  # caught up
    ran = RoundRunner(project, Publisher(private), now=NOW,
                      metrics=SlotMetrics([('cardbook', '2026-11-17T06:01:00+08:00')]))  # fmt: skip
    assert ran.phases_for(_at('13:15'), scheduled_at=_at('13:15')) == rest
    assert ran.phases_for(_at('14:00'), scheduled_at=_at('14:00')) == ('cardbook',)
    assert ran.phases_for(_at('06:00'), scheduled_at=_at('06:00')) == PHASES


def test_only_a_successful_slotted_run_covers_its_window(project, private):
    publisher = Publisher(private)
    outcomes = iter([1, 0])

    def run(phase, day, **kwargs):
        code = next(outcomes)
        return {'command': phase, 'game_day': day, 'started_at': '2026-11-17T14:00:05+08:00',
                'execution': 'failed' if code else 'awaiting_completion', 'exit_code': code}, None  # fmt: skip

    with patch('autocontrol.projects.yslzm.run_scheduled', side_effect=run):
        runner(project, publisher, now=_at('14:00')).run_once(
            now=_at('14:00'), phases=('cardbook',)
        )
        assert 'slot_runs' not in publisher.snapshots[-1]
        runner(project, publisher, now=_at('14:30')).run_once(
            now=_at('14:30'), phases=('cardbook',)
        )
    assert publisher.snapshots[-1]['slot_runs'] == {'cardbook': [1]}


def test_a_server_reported_wake_runs_only_the_activities_phase(project, private):
    ran = SlotMetrics(
        [('cardbook', f'2026-11-17T{hour}:01:00+08:00') for hour in ('06', '14', '22')]
    )
    wake = datetime.fromisoformat('2026-11-18T00:00:00+08:00')
    runner_ = RoundRunner(project, Publisher(private), now=NOW, metrics=ran)
    runner_.dynamic_candidates = (wake.timestamp(),)
    assert runner_.phases_for(wake, scheduled_at=wake) == ('activities',)
    near_reset = datetime.fromisoformat('2026-11-18T04:59:59+08:00')
    runner_.dynamic_candidates = (near_reset.timestamp(),)
    assert runner_.phases_for(near_reset, scheduled_at=near_reset) == ()  # quiet: nothing runs


class TaskDayMetrics(SlotMetrics):
    def __init__(self):
        super().__init__()
        self.days = []

    def record_task_days(self, project, day, rows, when):
        self.days.append((day, rows))


def test_task_days_carry_the_cards_verdict_and_failure_streaks(project, private):
    metrics = TaskDayMetrics()
    runner_ = RoundRunner(project, Publisher(private), now=NOW, metrics=metrics)
    view = project.new_projection(NOW, restore=False)
    view.apply_event({'kind': 'task_failed', 'task_id': 'guild', 'reason': 'action_exception'})
    view.apply_event({'kind': 'task_finished', 'task_id': 'bear', 'status': 'waiting',
                      'reason': 'quota_cap'})  # fmt: skip
    view.finish_run(NOW)
    runner_._record_task_days(view)
    ((day, rows),) = metrics.days
    assert rows['guild'] == {'status': 'failed', 'done': 0, 'fail_streak': 1}
    assert rows['bear'] == {'status': 'waiting', 'done': 1, 'fail_streak': None}


class Recorder:
    def __init__(self):
        self.snapshots = []
        self.status = {'status': 'sent'}

    def submit(self, snapshot):
        self.snapshots.append(snapshot)

    def flush(self, timeout=None):
        pass


def test_resuming_takes_up_the_saved_wakes_and_redraws_the_card_without_a_login(project, private):
    from datetime import timedelta

    publisher = Recorder()
    first = RoundRunner(project, publisher, now=NOW)
    view = project.new_projection(NOW, restore=False)
    wake = NOW + timedelta(hours=1)
    view.apply_event(
        {'kind': 'plan', 'plan': {'goals': {}, 'dynamic_candidates': [wake.timestamp()]}}
    )
    view.apply_event({'kind': 'task_finished', 'task_id': 'arena', 'status': 'complete'})
    first._publish(view)
    saved = publisher.snapshots[-1]

    def previous(self, day):
        return saved if day == saved['service_day'] else None

    later = RoundRunner(project, Recorder(), now=NOW)
    with patch.object(type(project), 'previous_display', previous):
        assert later.resume(NOW + timedelta(minutes=10)) is True
    assert later.dynamic_candidates == (wake.timestamp(),)
    drawn = later.publisher.snapshots[-1]
    assert drawn['discovered'] is True
    assert next(row for row in drawn['tasks'] if row['task_id'] == 'arena')['status'] == 'complete'


def test_a_new_day_without_a_login_is_a_planned_card_and_leaves_a_running_round_alone(
    project, private
):
    publisher = Recorder()
    runner_ = RoundRunner(project, publisher, now=NOW)
    reset = datetime.fromisoformat('2026-11-18T05:00:05+08:00')
    assert runner_.start_day(reset) is True
    card = publisher.snapshots[-1]
    assert card['service_day'] == '2026-11-18' and card['discovered'] is True
    assert all(row['status'] == 'not_started' for row in card['tasks'])
    held = locks.acquire(project.round_lock)
    try:
        assert runner_.start_day(reset) is False  # the running round draws it
    finally:
        os.close(held)
    assert len(publisher.snapshots) == 1
