"""autocontrol worker protocol, version 1.

This is the contract every onboarded automation script implements (see
``docs/接入规范.md``). It defines how a script is invoked, the progress events
it may stream, the final result it prints and the exit codes it returns. The
validators below are strict: anything outside the contract is dropped and
counted as a protocol violation, so an onboarded script can never leak free
text, identities or server responses into the board, the archive or Lark.
"""

from __future__ import annotations

from datetime import date
import json
import math
import re

from .board.catalog import STATUSES

VERSION = 1

# -- invocation ---------------------------------------------------------------

ENV_PROTOCOL = 'AUTOCONTROL_PROTOCOL'
ENV_PROJECT = 'AUTOCONTROL_PROJECT'
ENV_PHASE = 'AUTOCONTROL_PHASE'
ENV_MODE = 'AUTOCONTROL_MODE'  # execute | observe
ENV_SERVICE_DAY = 'AUTOCONTROL_SERVICE_DAY'
ENV_RUN_ID = 'AUTOCONTROL_RUN_ID'
ENV_DEADLINE = 'AUTOCONTROL_DEADLINE'
ENV_PROGRESS_FD = 'AUTOCONTROL_PROGRESS_FD'
MODES = ('execute', 'observe')

EXIT_OK = 0  # ran to the end; the final result is authoritative
EXIT_FAILED = 1  # failed; the result (if any) may be partial
EXIT_BUSY = 2  # the account/resource is used by another program; nothing was done
EXIT_CONFIG = 3  # configuration or precondition missing; nothing was done

# -- limits -----------------------------------------------------------------------

MAX_EVENT_BYTES = 4096  # one atomic pipe write (PIPE_BUF)
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_SCOPE_BYTES = 128 * 1024
MAX_WAKES = 48
MAX_TASKS = 256
MAX_VIOLATIONS = 64
TASK_ID = re.compile(r'[a-z][a-z0-9_]{0,39}')
ERROR_TYPE = re.compile(r'[A-Z][A-Za-z0-9]{0,63}')
MAX_EPOCH = 2**33

# -- vocabulary -------------------------------------------------------------------

EVENT_KINDS = ('plan', 'task_started', 'task_progress', 'task_finished', 'task_failed')
OUTCOMES = ('complete', 'partial', 'waiting', 'failed', 'busy', 'skipped')
TASK_FIELDS = ('status', 'reason', 'current', 'total', 'unit', 'next_run_at', 'counts')

STATUS_TEXT = {
    'not_started': '计划内，尚未处理',
    'running': '正在处理',
    'incomplete': '部分完成，或仍有可做的部分',
    'waiting': '需要等待时间（服务器计时、刷新、恢复）',
    'blocked': '条件不满足（资源不足、未授权等），本轮不再尝试',
    'complete': '有服务器证据表明已完成',
    'failed': '明确失败',
    'result_unavailable': '结果未知（请求已发出未收到结果，或状态读取失败）',
    'unsupported': '发现目标，但没有处理流程',
    'closed': '今日不适用（未开放、无资格），卡片隐藏且不计数',
}
assert set(STATUS_TEXT) == set(STATUSES)

FAILURE_STAGES = {
    'config': '配置或凭据缺失',
    'lock': '资源被其他程序占用',
    'login': '登录失败',
    'connect': '连接服务器失败',
    'read': '读取状态失败',
    'execute': '业务执行环节失败',
    'write': '结果写出失败',
    # Stages only the console assigns:
    'summary': '执行结果摘要未返回',
    'timeout': '执行超时',
    'protocol': '结果不符合接入协议',
    'process': '执行进程异常退出',
    'process_start': '执行进程启动失败',
    'project_cwd': '无法访问项目目录',
}
WORKER_FAILURE_STAGES = ('config', 'lock', 'login', 'connect', 'read', 'execute', 'write')

STANDARD_REASONS = {
    'request_result_missing': '请求已发出，但未收到结果',
    'action_exception': '执行时发生异常，未收到成功结果',
    'timeout': '执行超时，尚未收到结果',
    'session_interrupted': '会话已中断',
    'session_unavailable': '当前会话不可用，尚未执行',
    'state_unavailable': '服务器尚未提供该项状态',
    'login_required': '需要重新登录',
    'captcha_required': '需要人工验证',
    'rate_limited': '请求过于频繁，稍后再试',
    'server_maintenance': '服务器维护中',
    'config_missing': '缺少配置或凭据',
    'forbidden': '当前授权或规则不允许执行',
    'insufficient_currency': '货币不足',
    'insufficient_stamina': '体力不足',
    'insufficient_items': '道具不足',
    'insufficient_materials': '材料不足',
    'waiting_for_refresh': '等待服务器刷新',
    'waiting_for_completion': '等待服务器任务到期',
    'waiting_for_stamina': '等待体力恢复',
    'not_open': '尚未开放',
    'done_for_today': '今日已无可做，明日继续',
    'unsupported': '发现目标，但尚无处理流程',
}
STANDARD_SHORT_REASONS = {
    'login_required': '需要登录',
    'captcha_required': '需人工验证',
    'rate_limited': '频率受限',
    'server_maintenance': '服务器维护',
    'config_missing': '缺少配置',
    'forbidden': '未获授权',
    'insufficient_currency': '货币不足',
    'insufficient_stamina': '体力不足',
    'insufficient_items': '道具不足',
    'insufficient_materials': '材料不足',
    'waiting_for_stamina': '等待体力',
    'done_for_today': '明日继续',
}
DAY_DONE_REASONS = ('done_for_today',)
REQUEST_PROBLEMS = (
    'request_result_missing',
    'action_exception',
    'timeout',
    'session_interrupted',
    'ended_without_result',
)
STANDARD_UNITS = ('次', '项', '个', '份', '轮', '件', '套', '步', '封', '张', '点', '宝箱', '派遣')

# Operational metrics a script reports in its result (all non-negative ints).
METRIC_KEYS = {
    'actions_sent': '发出的变更请求数（领取、购买、提交等）',
    'actions_confirmed': '收到成功回执的变更请求数',
    'actions_failed': '被明确拒绝或失败的变更请求数',
    'actions_unconfirmed': '已发出但未收到结果的变更请求数',
    'reads': '只读查询请求数',
    'retries': '重试次数（含重连）',
    'sessions': '本次新建的登录会话或连接数',
}


class Violations:
    """Collects fixed-text protocol violations and ignored fields."""

    def __init__(self):
        self.errors: list[str] = []
        self.ignored: list[str] = []

    def add(self, code: str, where: str = '') -> None:
        if len(self.errors) < MAX_VIOLATIONS:
            self.errors.append(f'{code}@{where}' if where else code)

    def ignore(self, where: str) -> None:
        if len(self.ignored) < MAX_VIOLATIONS:
            self.ignored.append(where)

    def __bool__(self) -> bool:
        return bool(self.errors)


def _count(value) -> bool:
    return type(value) is int and 0 <= value <= 2**53


def _epoch(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 < value < MAX_EPOCH


def valid_day(value) -> bool:
    try:
        return type(value) is str and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _key(where: str, key) -> str:
    return f'{where}.{key}' if type(key) is str and TASK_ID.fullmatch(key) else f'{where}.?'


def task_state(value, catalog, report: Violations, where: str) -> dict | None:
    """Validate one TaskState object; unknown fields are ignored and noted."""
    if type(value) is not dict:
        report.add('task_state_type', where)
        return None
    state = {}
    for key in value:
        if key not in TASK_FIELDS:
            report.ignore(_key(where, key))
    status = value.get('status')
    if status is not None:
        if status in STATUSES:
            state['status'] = status
        else:
            report.add('status_unknown', where)
    reason = value.get('reason')
    if reason is not None:
        if type(reason) is str and reason in catalog.reasons:
            state['reason'] = reason
        else:
            report.add('reason_unknown', where)
    for field in ('current', 'total'):
        number = value.get(field)
        if number is not None:
            if _count(number):
                state[field] = number
            else:
                report.add('number_invalid', f'{where}.{field}')
    if state.get('total') == 0:
        state.pop('total')
    unit = value.get('unit')
    if unit is not None:
        if type(unit) is str and unit in catalog.units:
            state['unit'] = unit
        else:
            report.add('unit_unknown', where)
    when = value.get('next_run_at')
    if when is not None:
        if _epoch(when):
            state['next_run_at'] = when
        else:
            report.add('time_invalid', f'{where}.next_run_at')
    counts = value.get('counts')
    if counts is not None:
        if type(counts) is not dict:
            report.add('counts_type', where)
        else:
            clean = {}
            for name, number in counts.items():
                if type(name) is str and name in catalog.count_keys and _count(number):
                    clean[name] = number
                else:
                    report.add('count_invalid', _key(f'{where}.counts', name))
            if clean:
                state['counts'] = clean
    return state


def task_states(value, catalog, report: Violations, where: str) -> dict:
    if type(value) is not dict:
        report.add('tasks_type', where)
        return {}
    states = {}
    for task, source in list(value.items())[:MAX_TASKS]:
        if type(task) is not str or task not in catalog.tasks:
            report.add('task_unknown', _key(where, task))
            continue
        state = task_state(source, catalog, report, f'{where}.{task}')
        if state is not None:
            states[task] = state
    if len(value) > MAX_TASKS:
        report.add('too_many_tasks', where)
    return states


def wake_times(value, report: Violations, where: str) -> list:
    if value is None:
        return []
    if type(value) is not list or len(value) > MAX_WAKES:
        report.add('next_wake_invalid', where)
        return []
    good = [item for item in value if _epoch(item)]
    if len(good) != len(value):
        report.add('next_wake_invalid', where)
    return sorted(set(good))


def parse_event_line(line: bytes, catalog, phase: str, report: Violations) -> dict | None:
    """Validate one raw progress line; return the console's internal event."""
    if len(line) > MAX_EVENT_BYTES:
        report.add('event_too_large')
        return None
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        report.add('event_not_json')
        return None
    return parse_event(event, catalog, phase, report)


def parse_event(event, catalog, phase: str, report: Violations) -> dict | None:
    if type(event) is not dict:
        report.add('event_type')
        return None
    if event.get('v') != VERSION:
        report.add('event_version')
        return None
    kind = event.get('kind')
    if kind not in EVENT_KINDS:
        report.add('event_kind')
        return None
    seq, ts = event.get('seq'), event.get('ts')
    if not (type(seq) is int and 1 <= seq <= 2**53):
        report.add('event_seq', kind)
        return None
    if not _epoch(ts):
        report.add('event_ts', kind)
        return None
    run = event.get('run', 'worker')
    if type(run) is not str or not 1 <= len(run) <= 64:
        report.add('event_run', kind)
        run = 'worker'
    internal = {'kind': kind, 'phase': phase, 'sequence': seq, 'generation': run, 'observed_at': ts}
    day = event.get('day')
    if day is not None:
        if valid_day(day):
            internal['game_day'] = day
        else:
            report.add('event_day', kind)
    known = {'v', 'kind', 'seq', 'ts', 'run', 'day'}
    if kind == 'plan':
        for key in event:
            if key not in known | {'tasks', 'next_wake'}:
                report.ignore(_key('plan', key))
        plan = {
            'full_checklist_summary': {
                'goals': task_states(event.get('tasks'), catalog, report, 'plan')
            }
        }
        plan['dynamic_candidates'] = wake_times(event.get('next_wake'), report, 'plan.next_wake')
        if 'game_day' in internal:
            plan['game_day'] = internal['game_day']
        internal['plan'] = plan
        return internal
    task = event.get('task')
    if type(task) is not str or task not in catalog.tasks:
        report.add('task_unknown', _key(kind, task))
        return None
    for key in event:
        if key not in known | {'task'} | set(TASK_FIELDS):
            report.ignore(_key(kind, key))
    state = task_state(
        {k: event[k] for k in TASK_FIELDS if k in event}, catalog, report, f'{kind}.{task}'
    )
    internal.update(state or {}, task_id=task)
    if kind == 'task_failed':
        internal.setdefault('status', 'failed')
        if internal['status'] != 'failed':
            # e.g. result_unavailable: the projection keeps the given status.
            internal['kind'] = 'task_finished'
    elif kind == 'task_finished' and 'status' not in internal:
        report.add('status_missing', f'{kind}.{task}')
        return None
    return internal


def parse_result(
    value, catalog, phase: str, report: Violations, *, allow_scope: bool
) -> dict | None:
    """Validate the final result object of a phase; ``None`` if unusable."""
    if type(value) is not dict:
        report.add('result_type')
        return None
    if value.get('v') != VERSION:
        report.add('result_version')
        return None
    outcome = value.get('outcome')
    if outcome not in OUTCOMES:
        report.add('result_outcome')
        return None
    day = value.get('day')
    if not valid_day(day):
        report.add('result_day')
        return None
    if value.get('phase') != phase:
        report.add('result_phase')
    known = {
        'v',
        'phase',
        'day',
        'outcome',
        'failure_stage',
        'error_type',
        'tasks',
        'next_wake',
        'metrics',
        'run_scope',
    }
    for key in value:
        if key not in known:
            report.ignore(_key('result', key))
    result = {'outcome': outcome, 'day': day}
    stage = value.get('failure_stage')
    if stage is not None:
        if stage in WORKER_FAILURE_STAGES:
            result['failure_stage'] = stage
        else:
            report.add('failure_stage_unknown')
    error_type = value.get('error_type')
    if error_type is not None:
        if type(error_type) is str and ERROR_TYPE.fullmatch(error_type):
            result['error_type'] = error_type
        else:
            report.add('error_type_invalid')
    result['tasks'] = task_states(value.get('tasks', {}), catalog, report, 'result')
    result['next_wake'] = wake_times(value.get('next_wake'), report, 'result.next_wake')
    metrics = value.get('metrics')
    clean = {}
    if metrics is not None:
        if type(metrics) is not dict:
            report.add('metrics_type')
        else:
            for name, number in metrics.items():
                if name in METRIC_KEYS and _count(number):
                    clean[name] = number
                else:
                    report.add('metric_invalid', _key('metrics', name))
    result['metrics'] = clean
    scope = value.get('run_scope')
    if scope is not None:
        if not allow_scope:
            report.ignore('result.run_scope')
        elif (
            type(scope) is not dict
            or len(json.dumps(scope, separators=(',', ':'))) > MAX_SCOPE_BYTES
        ):
            report.add('run_scope_invalid')
        else:
            result['run_scope'] = scope
    return result
