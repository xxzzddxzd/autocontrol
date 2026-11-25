"""One project's long-running scheduler; the console process keeps it running.

On start the service runs a live catch-up round, so a window that opened while
the machine was off is not postponed. Started with ``observe_first`` it reads
the plan first instead; with ``resume`` (only the console code changed) it
reads nothing and waits for its next wake. Afterwards it wakes at the next
fixed time or server-observed candidate; a server time that passed while
later phases of the round still ran is run right after it (a catch-up
round never starts another one). The service-day reset wakes for
read-only discovery, unless a round follows soon: then the new day's card is
drawn from the manifest and that round reads the server. Failures back off
instead of hammering the game server. ``log`` gets one line per decision (the
console keeps them in ``logs/<project>.log``).
"""

from __future__ import annotations

import threading

from .locks import AlreadyRunning
from .schedule import SAME_TIME

BUSY_WAIT = 60.0
# A due round whose account is busy (e.g. another script on the same account)
# is retried every BUSY_WAIT seconds for this long before it is given up.
BUSY_RETRY = 1800.0
MAX_BACKOFF = 900.0
# A reset this close before the next round draws the new day without a login.
RESET_SKIP = 5400.0


def backoff(failures: int) -> float:
    return min(MAX_BACKOFF, BUSY_WAIT * 2 ** min(16, max(0, failures - 1)))


def _silent(text: str) -> None:
    pass


def _hm(when) -> str:
    try:
        return when.strftime('%H:%M')
    except (AttributeError, ValueError):
        return '?'


def _kind(plan, when, candidates) -> str:
    """What a wake is, for the log: a fixed time, a server time or a phase's own slot."""
    try:
        if plan.is_fixed(when):
            return '固定时刻'
        if plan.is_dynamic(when, candidates):
            return '服务器时刻'
    except Exception:
        return '时刻'
    return '阶段时刻'


def _following(plan, now, candidates):
    """The first ordinary wake after ``now`` (or None)."""
    try:
        return plan.next_check(now, candidates)
    except Exception:
        return None


def _late(plan, now, runner):
    """The earliest server time the last round reported that passed during it.

    Taken once from the runner. ``None`` when there is none, when another
    wake is due within SAME_TIME anyway (that round runs instead, so no fixed
    time is skipped), or for times in the reset's quiet window or an earlier
    service day.
    """
    take = getattr(runner, 'take_overdue', None)
    if take is None:
        return None
    try:
        day = plan.service_day(now)
        stamps = [
            stamp
            for stamp in plan.dynamic_stamps(take())
            if stamp <= now and plan.service_day(stamp) == day
        ]
        if not stamps:
            return None
        due, _ = plan.next_wake(now, runner.dynamic_candidates)
        if due - now <= SAME_TIME:
            return None
        return min(stamps)
    except Exception:
        return None


def _outcome(result, seconds) -> str:
    delivery = result.get('delivery') if type(result.get('delivery')) is dict else {}
    status = delivery.get('status', '—')
    card = '只存看板' if status == 'local' else status
    return (
        f"状态 {result.get('status')}，退出码 {result.get('code')}，"
        f"{result.get('phases', 0)} 个阶段，卡片 {card}，用时 {int(seconds)} 秒"
    )


def run_service(
    runner,
    *,
    stop=None,
    clock=None,
    observe_first=False,
    resume=False,
    tick=30.0,
    log=None,
    reset_skip=RESET_SKIP,
):
    stop = stop or threading.Event()
    schedule = runner.project.schedule
    clock = clock or schedule.now
    log = log or _silent
    observe = bool(observe_first)
    why = '启动后先只读读取' if observe else '启动补跑'
    plan = schedule
    scheduled = None  # the wake that is due; None is the start-up catch-up
    reset = False  # the due wake is the service-day reset
    waiting = False  # resumed: nothing runs before the next wake
    failures = 0
    busy_since = None
    catching_up = False  # the due round runs a server time that passed (see _late)
    if resume:
        try:
            runner.resume(clock())
            waiting = True
            log('沿用已保存的卡片，不登录，等下一轮')
        except Exception:
            observe, why = True, '沿用卡片失败，先只读读取'
    while not stop.is_set():
        began = clock()
        try:
            following = _following(plan, began, runner.dynamic_candidates) if reset else None
            if waiting:
                waiting = False
                result = {'status': 'resumed'}
            elif (
                reset
                and observe
                and following is not None
                and (following - began).total_seconds() <= reset_skip
            ):
                if runner.start_day(began):
                    log(f'换日：{_hm(following)} 就有一轮，不登录，按清单出新一天的卡片')
                else:
                    log('换日：有一轮正在运行，由它出新一天的卡片')
                result = {'status': 'day_started'}
            else:
                phases = None if observe else runner.phases_for(began, scheduled_at=scheduled)
                if phases == ():
                    result = {'status': 'idle'}  # nothing is due at this wake
                    log(f'{_hm(scheduled)} 没有到期的阶段，跳过')
                else:
                    named = f"，阶段 {'、'.join(map(str, phases))}" if phases else ''
                    log(f"开始{'只读' if observe else ''}一轮：{why}{named}")
                    result = runner.run_once(observe_only=observe, now=clock(), phases=phases)
                    log('一轮结束：' + _outcome(result, (clock() - began).total_seconds()))
        except AlreadyRunning:
            result = {'status': 'busy'}
        except Exception:
            # No raw exception or response is kept. Rediscover later; never
            # replay a business invocation whose outcome was not returned.
            result = {'status': 'runner_error'}
        if result.get('status') == 'busy':
            now = clock()
            if not observe:
                if busy_since is None:
                    log('账号正被占用，每分钟再试这一轮，最多 30 分钟')
                busy_since = busy_since or now
                if (now - busy_since).total_seconds() < BUSY_RETRY:
                    # Keep the due round: run it once the account is free.
                    stop.wait(BUSY_WAIT)
                    continue
            busy_since = None
            log('账号一直被占用，放弃这一轮，稍后只读读取')
            stop.wait(BUSY_WAIT)
            observe, why, reset = True, '账号被占用后重新读取', False
            continue
        busy_since = None
        if result.get('status') == 'runner_error' or (observe and result.get('code')):
            failures += 1
            pause = backoff(failures)
            log(f'失败（连续 {failures} 次），{int(pause)} 秒后只读重试')
            stop.wait(pause)
            observe, why, reset = True, '失败后重新读取', False
            continue
        failures = 0
        try:  # a phase whose work is out keeps only its idle times
            plan = runner.effective_schedule(clock())
        except Exception:
            plan = schedule
        late = _late(plan, clock(), runner)
        if late is not None and not catching_up:
            # A server time passed while later phases of this round still ran:
            # run it now, once.
            scheduled, reset, observe, catching_up = late, False, False, True
            why = f'补跑服务器时刻 {_hm(late)}'
            log(f'下一轮 马上（服务器时刻 {_hm(late)} 在这一轮里已过）')
            continue
        catching_up = False
        due, observe = plan.next_wake(clock(), runner.dynamic_candidates)
        scheduled, reset = due, observe
        why = '换日' if observe else f'{_kind(plan, due, runner.dynamic_candidates)} {_hm(due)}'
        log(f'下一轮 {_hm(due)}（{why}）')
        # Short, interruptible waits also follow wall-clock adjustments.
        while not stop.is_set() and clock() < due:
            stop.wait(min(tick, max(0.1, (due - clock()).total_seconds())))
    log('收到停止信号，调度退出')
