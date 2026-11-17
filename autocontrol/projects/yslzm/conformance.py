"""Static admission checks of the yslzm adapter against the live yslzm checkout.

yslzm is an adapter project: its worker speaks its own progress protocol. These checks prove that everything the current worker
source can report is understood by the adapter, so no event, status or job
silently disappears from the card (as happened when the bear job was added).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re

from . import summary
from .catalog import CATALOG

PHASE_BRANCH = re.compile(r"\s{4}(?:if|elif) command=='([a-z]+)':")
JOB_NAME = re.compile(r"\('([a-z_]+)',")


def _load(path: Path):
    """Import a stdlib-only worker module by path."""
    spec = importlib.util.spec_from_file_location('yslzm_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upstream_jobs(source: str) -> dict:
    """``{phase: [job, ...]}`` from the ``jobs=(...)`` tables of additional_daily_policies."""
    jobs, command, capture = {}, None, None
    for line in source.splitlines():
        branch = PHASE_BRANCH.match(line)
        if branch:
            command, capture = branch[1], None
            continue
        if 'jobs=(' in line and command:
            capture = command
            jobs[capture] = []
        if capture:
            if re.match(r'\s{4}(elif|else)\b', line) or re.match(
                r'\s{4}for name,run in jobs', line
            ):
                capture = None
                continue
            jobs[capture] += JOB_NAME.findall(line)
    return jobs


def _item(ident, title, passed, detail='', level='must'):
    return {'id': ident, 'level': level, 'title': title, 'passed': passed, 'detail': detail}


def check(workdir, *, phase_timeout=None) -> list[dict]:
    root = Path(workdir)
    items = []
    try:
        events = _load(root / 'business' / 'progress_events.py')
    except Exception as error:  # the worker source itself is broken or missing
        return [_item('Y0', '读取执行器进度上报定义', False, type(error).__name__)]
    tasks = sorted(
        t for t in set(events.TASKS) | set(events.ALIASES.values()) if CATALOG.task_id(t) is None
    )
    items.append(
        _item('Y1', '执行器可能上报的任务 ID 都在卡片目录中', not tasks, '未识别：' + '、'.join(tasks) if tasks else '')
    )
    states = sorted(state for state in events.STATES if not CATALOG.maps(state))
    items.append(
        _item('Y2', '执行器上报的状态都有明确映射', not states, '未映射：' + '、'.join(states) if states else '')
    )
    reasons = sorted(set(events.REASONS) - set(CATALOG.reasons))
    items.append(
        _item('Y3', '执行器上报的原因码都有中文说明', not reasons, '缺少：' + '、'.join(reasons) if reasons else '')
    )
    units = sorted(set(events.UNITS) - CATALOG.units)
    items.append(_item('Y4', '执行器使用的单位都被允许', not units, '缺少：' + '、'.join(units) if units else ''))
    try:
        jobs = upstream_jobs(
            (root / 'business' / 'additional_daily_policies.py').read_text(encoding='utf-8')
        )
    except OSError as error:
        jobs = {}
        items.append(_item('Y5', '各阶段任务表与适配器一致', False, type(error).__name__))
    else:
        problems = []
        for phase, names in sorted(jobs.items()):
            known = summary.PHASE_JOBS.get(phase, ())
            for name in names:
                spec = CATALOG.tasks.get(name)
                if name not in known:
                    problems.append(f'{phase}.{name} 不在结果摘要中')
                if spec is None or spec.kind != 'job' or spec.section != phase:
                    problems.append(f'{phase}.{name} 不在卡片目录中')
            problems += [f'{phase}.{name} 执行器已不再运行' for name in known if name not in names]
        items.append(_item('Y5', '各阶段任务表与适配器一致', bool(jobs) and not problems,
                           '；'.join(problems) if problems else '' if jobs else '未解析到任务表'))  # fmt: skip
    items.append(_item('Y6', '阶段有超时上限（有界执行）', bool(phase_timeout),
                       f'{int(phase_timeout)} 秒' if phase_timeout else '未设置'))  # fmt: skip
    items.append(_slots_item(workdir))
    return items


def _slots_item(workdir) -> dict:
    """Y7: the console's fixed slots equal the modules' schedules in the registry."""
    from .catalog import SCHEDULE

    title = '固定时段与模块注册表一致'
    path = Path(workdir) / 'business' / 'module_registry.py'
    try:
        registry = _load(path)
    except (OSError, ImportError, SyntaxError) as error:
        return _item('Y7', title, False, type(error).__name__)
    wanted = {item.section: tuple(item.schedule) for item in registry.MODULES if item.schedule}
    actual = SCHEDULE.slot_map
    problems = [
        f'{phase}：注册表 {"、".join(wanted.get(phase, ())) or "无"}，控制台 {"、".join(actual.get(phase, ())) or "无"}'
        for phase in sorted(set(wanted) | set(actual))
        if wanted.get(phase) != actual.get(phase)
    ]
    detail = (
        '；'.join(problems)
        if problems
        else '、'.join(f'{phase} {" ".join(times)}' for phase, times in sorted(actual.items()))
    )
    return _item('Y7', title, not problems, detail)
