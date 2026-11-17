"""Long-running scheduler: cron only keeps this process alive, it never runs phases.

On start the service runs a live catch-up round, so a window that opened while
the machine was off is not postponed. Afterwards it wakes at the next fixed
time or server-observed candidate; the service-day reset wakes for read-only
discovery only. Failures back off instead of hammering the game server.
"""

from __future__ import annotations

import threading

from .locks import AlreadyRunning

BUSY_WAIT = 60.0
# A due round whose account is busy (e.g. another script on the same account)
# is retried every BUSY_WAIT seconds for this long before it is given up.
BUSY_RETRY = 1800.0
MAX_BACKOFF = 900.0


def backoff(failures: int) -> float:
    return min(MAX_BACKOFF, BUSY_WAIT * 2 ** min(16, max(0, failures - 1)))


def run_service(runner, *, stop=None, clock=None, observe_first=False, tick=30.0):
    stop = stop or threading.Event()
    schedule = runner.project.schedule
    clock = clock or schedule.now
    observe = bool(observe_first)
    scheduled = None  # the wake that is due; None is the start-up catch-up
    failures = 0
    busy_since = None
    while not stop.is_set():
        try:
            phases = None if observe else runner.phases_for(clock(), scheduled_at=scheduled)
            if phases == ():
                result = {'status': 'idle'}  # nothing is due at this wake
            else:
                result = runner.run_once(observe_only=observe, now=clock(), phases=phases)
        except AlreadyRunning:
            result = {'status': 'busy'}
        except Exception:
            # No raw exception or response is kept. Rediscover later; never
            # replay a business invocation whose outcome was not returned.
            result = {'status': 'runner_error'}
        if result.get('status') == 'busy':
            now = clock()
            if not observe:
                busy_since = busy_since or now
                if (now - busy_since).total_seconds() < BUSY_RETRY:
                    # Keep the due round: run it once the account is free.
                    stop.wait(BUSY_WAIT)
                    continue
            busy_since = None
            stop.wait(BUSY_WAIT)
            observe = True
            continue
        busy_since = None
        if result.get('status') == 'runner_error' or (observe and result.get('code')):
            failures += 1
            stop.wait(backoff(failures))
            observe = True
            continue
        failures = 0
        due, observe = schedule.next_wake(clock(), runner.dynamic_candidates)
        scheduled = due
        # Short, interruptible waits also follow wall-clock adjustments.
        while not stop.is_set() and clock() < due:
            stop.wait(min(tick, max(0.1, (due - clock()).total_seconds())))
