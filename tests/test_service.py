from datetime import datetime, timedelta

from autocontrol.locks import AlreadyRunning
from autocontrol.projects.yslzm.catalog import SCHEDULE
from autocontrol.service import backoff, run_service
from autocontrol.schedule import BEIJING


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Stop:
    """Records waits and advances the fake clock; stops after ``limit`` rounds."""

    def __init__(self, clock, runner, limit):
        self.clock, self.runner, self.limit, self.waits = clock, runner, limit, []

    def is_set(self):
        return len(self.runner.calls) >= self.limit

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock.now += timedelta(seconds=seconds)


class Project:
    schedule = SCHEDULE


class Runner:
    project = Project()

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.phases = []
        self.dynamic_candidates = ()

    def phases_for(self, now, *, scheduled_at=None):
        return ('due-at', scheduled_at)

    def start_day(self, now):
        self.calls.append(('day', now))
        return True

    def resume(self, now):
        self.calls.append(('resume', now))
        return True

    def run_once(self, *, observe_only, now, phases=None):
        self.calls.append((observe_only, now))
        self.phases.append(phases)
        result = self.results.pop(0) if self.results else {'status': 'finished', 'code': 0}
        if isinstance(result, Exception):
            raise result
        return result


def serve(results, *, start, limit, observe_first=False, **options):
    clock = Clock(start)
    runner = Runner(results)
    stop = Stop(clock, runner, limit)
    run_service(runner, stop=stop, clock=clock, observe_first=observe_first, **options)
    return runner, stop


def test_startup_catch_up_round_then_sleeps_until_next_fixed_wake():
    start = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    runner, stop = serve([{'status': 'finished', 'code': 0}], start=start, limit=2)
    assert [call[0] for call in runner.calls] == [False, False]
    assert runner.calls[1][1] == start.replace(hour=13, minute=15)
    assert all(wait <= 30 for wait in stop.waits)


def test_a_busy_account_delays_the_due_round_instead_of_dropping_it():
    start = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    runner, stop = serve([{'status': 'busy'}, AlreadyRunning('round.lock')], start=start, limit=3)
    assert [call[0] for call in runner.calls] == [False, False, False]  # retried, still executing
    assert stop.waits[:2] == [60.0, 60.0]
    assert runner.phases[0] == runner.phases[2]  # the same wake's phases


def test_a_round_busy_for_half_an_hour_is_given_up_for_read_only_discovery():
    start = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    runner, stop = serve([{'status': 'busy'}] * 40, start=start, limit=33)
    flags = [call[0] for call in runner.calls]
    assert flags[:30] == [False] * 30 and flags[31] is True


def test_runner_errors_and_failed_discovery_back_off_exponentially():
    start = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    results = [
        RuntimeError('x'),
        {'status': 'observed', 'code': 1},
        {'status': 'observed', 'code': 1},
        {'status': 'observed', 'code': 0},
    ]
    runner, stop = serve(results, start=start, limit=5)
    assert stop.waits[:3] == [60.0, 120.0, 240.0]
    assert [call[0] for call in runner.calls] == [False, True, True, True, False]
    assert backoff(10) == 900.0


def test_day_reset_wakes_for_read_only_discovery_when_no_round_follows_soon():
    start = datetime(2026, 11, 17, 4, 40, tzinfo=BEIJING)
    runner, _ = serve([{'status': 'finished', 'code': 0}], start=start, limit=2, reset_skip=0)
    assert runner.calls[1] == (True, start.replace(hour=5, minute=0))


def test_a_reset_followed_soon_by_a_round_draws_the_new_day_without_a_login():
    start = datetime(2026, 11, 17, 4, 40, tzinfo=BEIJING)
    reset = start.replace(hour=5, minute=0)
    first = SCHEDULE.next_check(reset)
    assert (first - reset).total_seconds() <= 5400
    lines = []
    runner, _ = serve([], start=start, limit=3, log=lines.append)
    assert runner.calls == [(False, start), ('day', reset), (False, first)]
    assert f'换日：{first:%H:%M} 就有一轮，不登录，按清单出新一天的卡片' in lines


def test_resuming_after_new_console_code_waits_for_the_next_wake_without_a_login():
    start = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    lines = []
    runner, _ = serve([], start=start, limit=2, resume=True, log=lines.append)
    assert runner.calls == [('resume', start), (False, start.replace(hour=13, minute=15))]
    assert lines[0] == '沿用已保存的卡片，不登录，等下一轮'
    assert '下一轮 13:15（固定时刻 13:15）' in lines


def test_the_log_says_why_each_round_runs_and_how_it_ended():
    start = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    lines = []
    results = [{'status': 'finished', 'code': 0, 'phases': 2, 'delivery': {'status': 'sent'}}]
    serve(results, start=start, limit=1, log=lines.append)
    assert lines[0].startswith('开始一轮：启动补跑')
    assert lines[1] == '一轮结束：状态 finished，退出码 0，2 个阶段，卡片 sent，用时 0 秒'
    assert lines[2] == '下一轮 13:15（固定时刻 13:15）'


class LateRunner(Runner):
    """Each round takes ``minutes``; ``late`` lists, per round, the server
    times its phases reported that passed during it."""

    def __init__(self, clock, late, minutes):
        super().__init__([])
        self.clock, self.late, self.minutes, self.overdue = clock, list(late), minutes, ()

    def run_once(self, *, observe_only, now, phases=None):
        result = super().run_once(observe_only=observe_only, now=now, phases=phases)
        self.clock.now += timedelta(minutes=self.minutes)
        self.overdue = tuple(self.late.pop(0)) if self.late else ()
        return result

    def take_overdue(self):
        found, self.overdue = self.overdue, ()
        return found


def serve_late(late, *, start, limit, minutes=5):
    clock = Clock(start)
    runner = LateRunner(clock, late, minutes)
    run_service(runner, stop=Stop(clock, runner, limit), clock=clock)
    return runner


def test_a_server_time_that_passed_during_the_round_runs_right_after_it_once():
    start = datetime(2026, 11, 17, 10, 0, tzinfo=BEIJING)
    first, second = start + timedelta(minutes=3), start + timedelta(minutes=8)
    runner = serve_late([(first.timestamp(),), (second.timestamp(),)], start=start, limit=3)
    fixed = start.replace(hour=13, minute=15)
    # The 10:03 time passed while the round ran: it runs at 10:05 with that
    # time as its wake. The catch-up round's own passed time starts nothing.
    assert [call[1] for call in runner.calls] == [start, start + timedelta(minutes=5), fixed]
    assert runner.phases[1] == ('due-at', first)
    assert runner.phases[2] == ('due-at', fixed)


def test_a_passed_server_time_is_left_to_a_round_that_is_due_soon_anyway():
    start = datetime(2026, 11, 17, 13, 9, tzinfo=BEIJING)
    runner = serve_late([((start + timedelta(minutes=2)).timestamp(),)], start=start, limit=2)
    # The round ends 13:14 and the 13:15 round is due within two minutes: it runs instead.
    assert [call[1] for call in runner.calls] == [start, start.replace(minute=15)]
    assert runner.phases[1] == ('due-at', start.replace(minute=15))


class SlotRunner(Runner):
    """Each round takes ``minutes``; ``missed`` lists, per round, the slotted
    phases whose slot time passed during it."""

    def __init__(self, clock, missed, minutes):
        super().__init__([])
        self.clock, self.missed, self.minutes, self.since = clock, list(missed), minutes, []

    def run_once(self, *, observe_only, now, phases=None):
        result = super().run_once(observe_only=observe_only, now=now, phases=phases)
        self.clock.now += timedelta(minutes=self.minutes)
        return result

    def missed_slots(self, since, now=None):
        self.since.append(since)
        return tuple(self.missed.pop(0)) if self.missed else ()


def test_a_slot_time_that_passed_during_the_round_runs_right_after_it_once():
    start = datetime(2026, 11, 17, 13, 15, tzinfo=BEIJING)
    clock = Clock(start)
    runner = SlotRunner(clock, [('cardbook',), ('cardbook',)], minutes=59)
    lines = []
    run_service(runner, stop=Stop(clock, runner, 3), clock=clock, log=lines.append)
    after = start + timedelta(minutes=59)
    # The 14:00 slot passed while the 13:15 round ran: the cardbook runs at 14:14,
    # once; the catch-up round's own miss starts nothing, so 21:15 is next.
    assert [call[1] for call in runner.calls] == [start, after, start.replace(hour=21)]
    assert runner.phases[1] == ('due-at', after)
    assert '下一轮 马上（cardbook 的阶段时刻在这一轮里已过）' in lines
    assert any(line.startswith('开始一轮：补跑阶段时刻（cardbook）') for line in lines)
