"""A project's daily checklist described as data.

The catalog names every task the board may show, the section (phase) it is
listed under, and the vocabulary used to translate worker statuses and reason
codes into the board's ten display states. Unknown task ids, statuses and
reasons never reach the board.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping

# Display states, in no particular order.
STATUSES = (
    'not_started',
    'incomplete',
    'running',
    'complete',
    'waiting',
    'blocked',
    'failed',
    'result_unavailable',
    'unsupported',
    'closed',
)
# When a raw worker status appears in several groups, the earlier group wins.
GROUP_ORDER = (
    'complete',
    'closed',
    'waiting',
    'blocked',
    'unsupported',
    'failed',
    'result_unavailable',
    'incomplete',
    'not_started',
)
# Reason codes the generic board itself produces.
BOARD_REASONS = {
    'failure': '执行失败，未收到该项成功结果',
    'state_unavailable': '服务器尚未提供该项状态',
    'unsupported': '尚无处理流程',
    'ended_without_result': '执行已结束，但该项未返回结果',
    'worker_unavailable': '执行器未返回状态',
    'completed_observation_retained': '本日已确认完成；本次未返回新状态',
    'round_harvested': '本轮已完成，下一轮继续',
    'window_ended': '今天的开放时段已结束，未完成',
}
# Short labels the board itself uses on a task line.
BOARD_SHORT_REASONS = {'window_ended': '已错过'}
# What creates the work, and what finishes today's part (manifest task fields).
DEMANDS = ('daily', 'weekly', 'window')
ENDPOINTS = ('target', 'once', 'claim')
DEFAULT_REASON = {
    'failed': 'failure',
    'result_unavailable': 'state_unavailable',
    'unsupported': 'unsupported',
}


@dataclass(frozen=True)
class TaskSpec:
    """One checklist entry.

    ``kind`` is ``goal`` (a daily target), ``job`` (a concrete piece of a phase)
    or ``step`` (a sub-step shown under its ``parent``; it never proves the
    parent complete). ``seed`` tasks are listed in the fixed plan before any
    worker reports. ``progress`` pairs derive ``current/total`` from counts.
    """

    key: str
    title: str
    section: str = ''
    kind: str = 'goal'
    parent: str | None = None
    seed: bool = False
    progress: tuple[tuple[str, str], ...] = ()
    unit: str | None = None
    next_run_count: str | None = None
    # 需求方: 'window' work exists only while the server opens it today.
    demand: str | None = None
    endpoint: str | None = None  # 今日终点: 'target', 'once' or 'claim'
    discovery: bool = False  # counts open activities no module handles


@dataclass(frozen=True)
class Section:
    """A group of tasks, normally one worker phase.

    ``aggregate`` names a goal that stands in for the section's jobs until the
    phase reports them individually; then the jobs replace it on the board.
    """

    key: str
    title: str
    aggregate: str | None = None


class Catalog:
    def __init__(
        self,
        *,
        project: str,
        display_name: str,
        sections: Iterable[Section],
        tasks: Iterable[TaskSpec],
        phases: Mapping[str, str],
        phase_sections: Mapping[str, str] | None = None,
        aliases: Mapping[str, str] | None = None,
        status_groups: Mapping[str, Iterable[str]] | None = None,
        status_hook: Callable[[str, dict], str | None] | None = None,
        reasons: Mapping[str, str] | None = None,
        short_reasons: Mapping[str, str] | None = None,
        request_problems: Iterable[str] = (),
        count_keys: Iterable[str] = (),
        units: Iterable[str] = (),
        plan_kinds: Iterable[str] = ('goal',),
        day_done_reasons: Iterable[str] = (),
        round_tasks: Mapping[str, Iterable[str]] | None = None,
    ):
        self.project = project
        self.display_name = display_name
        self.sections = tuple(sections)
        section_keys = {section.key for section in self.sections}
        specs = list(tasks)
        by_key = {spec.key: spec for spec in specs}
        if len(by_key) != len(specs):
            raise ValueError('duplicate task key')
        resolved = {}
        for spec in specs:
            if spec.kind not in ('goal', 'job', 'step'):
                raise ValueError(f'bad task kind for {spec.key}')
            if spec.kind == 'step':
                parent = by_key.get(spec.parent)
                if parent is None or parent.kind == 'step':
                    raise ValueError(f'step {spec.key} needs a goal or job parent')
                spec = replace(spec, section=spec.section or parent.section)
            if spec.section not in section_keys:
                raise ValueError(f'unknown section for {spec.key}')
            resolved[spec.key] = spec
        self.tasks: dict[str, TaskSpec] = resolved
        for section in self.sections:
            if section.aggregate is not None and self.tasks.get(section.aggregate) is None:
                raise ValueError(f'unknown aggregate {section.aggregate}')
        self.order = {key: index for index, key in enumerate(self.tasks)}
        self.phases = dict(phases)
        self.phase_sections = {
            phase: (phase_sections or {}).get(phase, phase)
            for phase in self.phases
            if (phase_sections or {}).get(phase, phase) in section_keys
        }
        self.aliases = dict(aliases or {})
        self.status_hook = status_hook
        self._status = {state: state for state in STATUSES}
        for group in GROUP_ORDER:
            for raw in (status_groups or {}).get(group, ()):
                self._status.setdefault(raw, group)
        self.reasons = {**BOARD_REASONS, **(reasons or {})}
        self.short_reasons = {**BOARD_SHORT_REASONS, **(short_reasons or {})}
        self.request_problems = frozenset(request_problems)
        self.count_keys = frozenset(count_keys)
        self.units = frozenset(units)
        # Task kinds a read-only plan may report (adapters: goals only).
        self.plan_kinds = frozenset(plan_kinds)
        # Reasons meaning nothing more can happen before the next service day.
        self.day_done_reasons = frozenset(day_done_reasons)
        # Per-round tasks (收菜): their state regenerates between rounds, so a
        # round whose own job harvested them counts them done until the next
        # round. Values are extra raw statuses/reasons that mean "harvested"
        # besides a complete state (e.g. stamina used up for a consumer). A
        # step key stands for its parent when the parent's own job reports
        # under the step's id (e.g. 代言维护 for 代言).
        self.round_tasks = {key: frozenset(extra) for key, extra in (round_tasks or {}).items()}
        if any(key not in self.tasks for key in self.round_tasks):
            raise ValueError('unknown round task')

    def task_id(self, value) -> str | None:
        """Map a worker task name (with ``_actions`` suffix or alias) to a key."""
        if type(value) is not str:
            return None
        if value.endswith('_actions'):
            value = value[: -len('_actions')]
        value = self.aliases.get(value, value)
        return value if value in self.tasks else None

    def title(self, key: str) -> str:
        return self.tasks[key].title

    def normalize(self, raw, counts=None) -> str:
        """Translate a raw worker status into one of :data:`STATUSES`."""
        if type(raw) is not str:
            return 'result_unavailable'
        if raw in self._status:
            return self._status[raw]
        if self.status_hook is not None:
            state = self.status_hook(raw, counts or {})
            if state in STATUSES:
                return state
        return 'result_unavailable'

    def maps(self, raw) -> bool:
        """Whether a raw worker status has an explicit display mapping."""
        if type(raw) is not str:
            return False
        if raw in self._status:
            return True
        return self.status_hook is not None and self.status_hook(raw, {}) in STATUSES

    def reason(self, source: dict, state: str) -> str | None:
        """A known reason code for this observation, or the state's default."""
        for field in ('reason', 'status'):
            value = source.get(field)
            if type(value) is str and value in self.reasons:
                return value
        return DEFAULT_REASON.get(state)

    def section_tasks(self, section: str) -> list[str]:
        return [key for key, spec in self.tasks.items() if spec.section == section]
