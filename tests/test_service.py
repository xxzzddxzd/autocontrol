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

    def run_once(self, *, observe_only, now, phases=None):
        self.calls.append((observe_only, now))
        self.phases.append(phases)
        result = self.results.pop(0) if self.results else {'status': 'finished', 'code': 0}
        if isinstance(result, Exception):
            raise result
        return result


def serve(results, *, start, limit, observe_first=False):
    clock = Clock(start)
    runner = Runner(results)
    stop = Stop(clock, runner, limit)
    run_service(runner, stop=stop, clock=clock, observe_first=observe_first)
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


def test_day_reset_wakes_for_read_only_discovery():
    start = datetime(2026, 11, 17, 4, 40, tzinfo=BEIJING)
    runner, _ = serve([{'status': 'finished', 'code': 0}], start=start, limit=2)
    assert runner.calls[1] == (True, start.replace(hour=5, minute=0))
