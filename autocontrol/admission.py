"""Admission check (准入检查) for protocol projects.

``autocontrol check --manifest <file>`` validates the manifest, then runs the
script once in read-only ``observe`` mode and grades the run against the
admission criteria of ``docs/接入规范.md``. A passing record, bound to the
manifest digest, is stored under ``<private>/admission/``; ``serve`` and
``once`` refuse a protocol project whose current manifest has not passed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil

from . import protocol
from .locks import is_free
from .projects.manifest import ManifestError, ManifestProject, load_manifest

CRITERIA = {
    'M1': ('must', '接入清单合法'),
    'M2': ('must', '工作目录与启动命令可用'),
    'M3': ('must', '只读发现正常结束（退出码 0，未超时）'),
    'M4': ('must', '最终结果符合协议'),
    'M5': ('must', '进度事件符合协议（含 plan 事件）'),
    'M6': ('must', '计划覆盖清单中的全部固定任务'),
    'M7': ('must', '只读模式未发出变更请求（metrics.actions_sent = 0）'),
    'M8': ('must', '等待开放的窗口型任务带开放时刻，且该时刻在 next_wake 中'),
    'M9': ('must', '声明的账号锁可以探测'),
    'S1': ('should', '未输出协议外字段'),
    'S2': ('should', '只读发现耗时不超过超时上限的一半'),
    'S3': ('should', '上报操作指标（actions_sent、reads 等）'),
    'S4': ('should', '重复只读发现的任务集合一致'),
    'S5': ('should', '有窗口型任务时，声明并上报「开放活动」发现任务'),
}


def _item(ident, passed, detail=''):
    level, title = CRITERIA[ident]
    return {'id': ident, 'level': level, 'title': title, 'passed': passed, 'detail': detail}


LOCK_BUSY = '当前被占用'


def _probe_account_lock(project) -> tuple[bool, str]:
    """Whether the declared account lock can be probed, and what the probe saw."""
    account = project.account_lock
    if account is None:
        return True, '未声明'
    try:
        return True, '' if is_free(account) else LOCK_BUSY
    except (OSError, ValueError) as error:
        why = str(error) if isinstance(error, ValueError) else type(error).__name__
        return False, f'无法探测：{why}'


def _command_available(project) -> tuple[bool, str]:
    if not project.workdir.is_dir():
        return False, f'工作目录不存在：{project.workdir}'
    program = project.argv(project.status_phase, 'observe', '2000-01-01')[0]
    if '/' in program:
        path = Path(program).expanduser()
        path = path if path.is_absolute() else project.workdir / path
        return (path.exists(), '' if path.exists() else f'找不到可执行文件：{program}')
    found = shutil.which(program)
    return (found is not None, '' if found else f'PATH 中找不到命令：{program}')


def _reported_states(record, details) -> dict:
    """Latest reported state per task: plan, then task events, then the result."""
    states = {}
    for event in details['events']:
        if event['kind'] == 'plan':
            states.update(event['plan']['full_checklist_summary']['goals'])
        elif event.get('task_id'):
            states[event['task_id']] = {**states.get(event['task_id'], {}), **event}
    states.update(record.get('tasks') or {})
    return states


def _window_checks(project, record, details) -> list:
    """M8 and S5 for tasks whose work exists only while the server opens it."""
    tasks = project.catalog.tasks
    windows = sorted(key for key, spec in tasks.items() if spec.demand == 'window')
    if not windows:
        return [_item('M8', True, '无窗口型任务'), _item('S5', True, '无窗口型任务')]
    states = _reported_states(record, details)
    wakes = set(record.get('next_wake') or ())
    for event in details['events']:
        if event['kind'] == 'plan':
            wakes |= set(event['plan'].get('dynamic_candidates') or ())
    problems = []
    for key in windows:
        state = states.get(key) or {}
        if state.get('status') != 'waiting':
            continue
        when = state.get('next_run_at')
        if type(when) not in (int, float):
            problems.append(f'{key} 缺少 next_run_at')
        elif not any(abs(when - wake) <= 60 for wake in wakes):
            problems.append(f'{key} 的开放时刻不在 next_wake 中')
    discovery = next((key for key, spec in tasks.items() if spec.discovery), None)
    found = discovery is not None and discovery in states
    return [
        _item('M8', not problems, '；'.join(problems[:8])),
        _item('S5', found, '' if found else
              '清单缺少 discovery 任务' if discovery is None else f'未上报 {discovery}'),  # fmt: skip
    ]


def _observed_tasks(record, details) -> set:
    tasks = set(record.get('tasks') or ())
    for event in details['events']:
        if event['kind'] == 'plan':
            tasks |= set(event['plan']['full_checklist_summary']['goals'])
        elif event.get('task_id'):
            tasks.add(event['task_id'])
    return tasks


def run_check(manifest, *, workdir=None, private=None, live=True, repeat=1, save=True) -> dict:
    report = {
        'manifest': str(Path(manifest).expanduser()),
        'protocol': protocol.VERSION,
        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'criteria': [],
    }
    try:
        load_manifest(manifest)
        project = ManifestProject(manifest, workdir=workdir, private=private)
    except ManifestError as error:
        report['criteria'].append(_item('M1', False, '；'.join(error.problems[:10])))
        report['passed'] = False
        return report
    report.update(project=project.name, digest=project.digest)
    criteria = report['criteria']
    criteria.append(_item('M1', True))
    available, why = _command_available(project)
    criteria.append(_item('M2', available, why))
    lock_item = _item('M9', *_probe_account_lock(project))
    if not live or not available:
        for ident in ('M3', 'M4', 'M5', 'M6', 'M7', 'M8'):
            criteria.append(_item(ident, False, '未执行只读发现' if not live else '启动命令不可用'))
        criteria.append(lock_item)
        report['passed'] = False
        return report

    day = project.schedule.service_day()
    record, details = project.invoke(project.status_phase, 'observe', day)
    violations = record['protocol']['violations']
    report['observe'] = {
        key: record.get(key) for key in ('exit_code', 'execution', 'failure_stage', 'duration_ms')
    } | {
        'events': details['total'],
        'events_rejected': details['rejected'],
        'violations': violations,
    }
    busy = record.get('execution') == 'busy'
    ok_run = record.get('exit_code') == 0 and record.get('execution') not in (
        'failed',
        'timeout',
        'unverified',
        'busy',
    )
    criteria.append(_item('M3', ok_run, '资源被占用，请稍后重试' if busy else
                          '' if ok_run else f"执行结果：{record.get('execution')}，阶段：{record.get('failure_stage')}"))  # fmt: skip
    result_errors = details['result_report'].errors
    criteria.append(
        _item(
            'M4',
            record['protocol']['result_valid'] and not result_errors,
            '；'.join(result_errors[:8]),
        )
    )
    event_errors = details['report'].errors
    has_plan = any(event['kind'] == 'plan' for event in details['events'])
    criteria.append(_item(
        'M5', has_plan and not event_errors and not details['rejected'],
        '；'.join((['缺少 plan 事件'] if not has_plan else []) + event_errors[:8]),
    ))  # fmt: skip
    seeded = {key for key, spec in project.catalog.tasks.items() if spec.seed}
    missing = sorted(seeded - _observed_tasks(record, details))
    criteria.append(_item('M6', not missing, ('未覆盖：' + '、'.join(missing[:12])) if missing else ''))
    sent = (record.get('metrics') or {}).get('actions_sent')
    criteria.append(_item('M7', sent == 0, '未上报 metrics.actions_sent' if sent is None else
                          '' if sent == 0 else f'只读模式发出了 {sent} 个变更请求'))  # fmt: skip
    window_m8, window_s5 = _window_checks(project, record, details)
    criteria.append(window_m8)
    criteria.append(lock_item)
    ignored = details['report'].ignored + details['result_report'].ignored
    criteria.append(_item('S1', not ignored, '；'.join(ignored[:8])))
    duration = record.get('duration_ms') or 0
    criteria.append(_item('S2', duration <= project.observe_timeout * 500,
                          f'{duration} ms / 上限 {int(project.observe_timeout * 1000)} ms'))  # fmt: skip
    metrics = record.get('metrics') or {}
    lacking = sorted({'actions_sent', 'reads'} - set(metrics))
    criteria.append(_item('S3', not lacking, ('缺少：' + '、'.join(lacking)) if lacking else ''))
    if repeat > 1:
        first = _observed_tasks(record, details)
        again, more = project.invoke(project.status_phase, 'observe', day)
        same = _observed_tasks(again, more) == first
        criteria.append(_item('S4', same, '' if same else '两次只读发现报告的任务集合不同'))
    criteria.append(window_s5)
    report['passed'] = all(item['passed'] for item in criteria if item['level'] == 'must')
    if save:
        report['saved_to'] = str(save_record(project, report))
    return report


UNKNOWN_IS_EXPECTED = frozenset(('unknown', 'uncertain', 'timeout'))


def run_adapter_check(project, *, live=True, save=True) -> dict:
    """Admission check of an adapter project: static conformance + a read-only run."""
    report = {
        'project': project.name,
        'mode': 'adapter',
        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'criteria': list(project.conformance()) if hasattr(project, 'conformance') else [],
    }
    criteria = report['criteria']
    if live:
        seen = {'events': 0, 'plans': 0, 'unknown_tasks': set(), 'unmapped': set()}
        catalog = project.catalog

        def collect(event):
            seen['events'] += 1
            if event.get('kind') == 'plan':
                seen['plans'] += 1
            task = event.get('task_id')
            if task is not None and catalog.task_id(task) is None:
                seen['unknown_tasks'].add(str(task)[:40])
            status = event.get('status')
            if (
                status is not None
                and not catalog.maps(status)
                and status not in UNKNOWN_IS_EXPECTED
            ):
                seen['unmapped'].add(str(status)[:40])

        probed, seen_lock = _probe_account_lock(project)
        if not probed or seen_lock == LOCK_BUSY:
            criteria.append(_adapter_item('L1', '只读发现正常结束', False,
                                          '账号正被其他程序使用，请稍后重试' if probed else seen_lock))  # fmt: skip
        else:
            import time as _time

            start = _time.monotonic()
            try:
                code, record = project.observe(project.schedule.service_day(), on_event=collect,
                                               timeout=project.observe_timeout)  # fmt: skip
            except Exception as error:
                code, record = 1, {'failure_stage': type(error).__name__}
            duration = int((_time.monotonic() - start) * 1000)
            busy = project.is_busy(record)
            criteria.append(_adapter_item('L1', '只读发现正常结束', code == 0 and not busy,
                                          '' if code == 0 else f"退出码 {code}，阶段 {record.get('failure_stage')}"))  # fmt: skip
            criteria.append(
                _adapter_item('L2', '收到计划事件', seen['plans'] > 0, f"事件 {seen['events']} 条")
            )
            odd = sorted(seen['unknown_tasks']) + sorted(seen['unmapped'])
            criteria.append(_adapter_item('L3', '实际上报的任务与状态都能识别', not odd, '；'.join(odd[:12])))
            criteria.append(_adapter_item('L4', '只读发现耗时不超过超时上限的一半',
                                          duration <= project.observe_timeout * 500, f'{duration} ms', level='should'))  # fmt: skip
    report['passed'] = all(item['passed'] for item in criteria if item['level'] == 'must')
    if save:
        report['saved_to'] = str(save_record(project, report))
    return report


def _adapter_item(ident, title, passed, detail='', level='must'):
    return {'id': ident, 'level': level, 'title': title, 'passed': passed, 'detail': detail}


def record_path(project) -> Path:
    return record_file(project.private, project.name)


def record_file(private, name) -> Path:
    return Path(private) / 'admission' / f'{name}.json'


def passed_digest(private, name) -> str | None:
    """The manifest digest of the project's last admission record, when it passed."""
    try:
        record = json.loads(record_file(private, name).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if type(record) is not dict or record.get('passed') is not True:
        return None
    return record['digest'] if type(record.get('digest')) is str else None


def save_record(project, report) -> Path:
    path = record_path(project)
    # serve refuses a private directory that is not 0700, and mkdir(parents=True)
    # would create it with the umask's mode, so create it explicitly first.
    project.private.parent.mkdir(parents=True, exist_ok=True)
    project.private.mkdir(mode=0o700, exist_ok=True)
    path.parent.mkdir(exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name('.' + path.name + '.tmp')
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600
    )
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write('\n')
    os.replace(temporary, path)
    return path


def admitted(project) -> tuple[bool, str]:
    """Whether the current manifest of a protocol project passed admission."""
    try:
        record = json.loads(record_path(project).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False, f'尚未通过准入检查，请先运行：autocontrol check --manifest {project.manifest_path}'
    if record.get('digest') != project.digest:
        return False, '接入清单已变更，请重新运行准入检查'
    if not record.get('passed'):
        return False, '最近一次准入检查未通过，请查看 ' + str(record_path(project))
    return True, ''


def format_report(report) -> str:
    lines = [f"准入检查：{report.get('project', '?')}  {'通过' if report.get('passed') else '未通过'}"]
    for item in report['criteria']:
        mark = '✓' if item['passed'] else ('✗' if item['level'] == 'must' else '△')
        level = '必须' if item['level'] == 'must' else '建议'
        line = f"  {mark} {item['id']} [{level}] {item['title']}"
        if item['detail']:
            line += f"\n      {item['detail']}"
        lines.append(line)
    if report.get('observe'):
        observe = report['observe']
        lines.append(
            f"  只读发现：退出码 {observe['exit_code']}，结果 {observe['execution']}，耗时 {observe['duration_ms']} ms，"
            f"事件 {observe['events']}（拒绝 {observe['events_rejected']}）"
        )
    if report.get('saved_to'):
        lines.append(f"  记录：{report['saved_to']}")
    return '\n'.join(lines)
