"""Lark / Feishu card (JSON 2.0) for one service day: a to-do list with its schedule.

Layout::

    header   以闪亮之名 · 11月17日 周二                  [执行中] [1 项异常]
             每日清单 · 已完成 17/27
    plan     执行计划 · 今日还剩 6 轮
             12:04 事件簿到期 · 13:15 · 17:15 · 21:15 · 凌晨 01:15 · 凌晨 04:30（最后一轮）
             05:00 读取新一天计划 · 明日 06:00 首轮
    section  日常 7/11 · 1 项异常                                     (expanded)
             ☐ 红点奖励  状态未返回
                 服务器尚未提供该项状态 · 今日 13:15 核对
             ✗ 宠物  失败
                 执行时发生异常，未收到成功结果 · 今日 13:15 重试
             ▸ 已完成 7 项                                           (nested, collapsed)
    section  制作 4/4 ✓                                               (collapsed)
    详情 (collapsed)  sub-step observations without a result
    footer   更新 11:52

Only unfinished items are listed openly, in fixed business order; finished
items are folded into their section. An item whose reason says nothing more
can happen before the next service day (e.g. 今日已成长) counts as done today:
it is folded and counted like a completed one. Every unfinished item says why
it is not done and when it will be executed next (today, after midnight, or
tomorrow). Failures carry a red cross. Rendering is pure: the same snapshot
always gives the same card, and the schedule is computed from the snapshot's
own clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json

from .catalog import STATUSES, Catalog
from ..schedule import Schedule

CARD_LIMIT = 28 * 1024
WEEKDAYS = '一二三四五六日'
# Read-only status icons (a todolist look without a clickable box): ○ pending,
# ◉ running, ✓ done, ✗ failed, ! a request whose result never came back.
PENDING_ICON = {'tag': 'standard_icon', 'token': 'ellipse_outlined', 'color': 'grey'}
RUNNING_ICON = {'tag': 'standard_icon', 'token': 'record_outlined', 'color': 'blue'}
DONE_ICON = {'tag': 'standard_icon', 'token': 'succeed_filled', 'color': 'green'}
FAILED_ICON = {'tag': 'standard_icon', 'token': 'error_filled', 'color': 'red'}
UNKNOWN_ICON = {'tag': 'standard_icon', 'token': 'warning-hollow_filled', 'color': 'orange'}
STATE_TEXT = {
    'waiting': '等待中',
    'blocked': '条件暂不满足',
    'not_started': '尚未执行',
    'incomplete': '尚未完成',
    'result_unavailable': '状态未返回',
    'failed': '执行失败',
    'unsupported': '暂无处理流程',
}
VERBS = {
    'not_started': '执行',
    'incomplete': '执行',
    'blocked': '再试',
    'failed': '重试',
    'result_unavailable': '核对',
    'waiting': '再试',
}
SAME_TIME = timedelta(seconds=120)
# States in which a day-done reason means today's part is finished.
SETTLED_STATES = ('incomplete', 'waiting', 'blocked')


@dataclass
class Item:
    """A primary checklist row with the sub-steps reported under it."""

    key: str
    row: dict
    children: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    active: dict | None = None
    settled: bool = False  # done for today; the target continues next service day
    runs: tuple | None = None  # (successful slot windows, slots) of a slotted task
    slot_done: frozenset = frozenset()  # indexes of those windows

    @property
    def status(self) -> str:
        return self.row['status']

    @property
    def done(self) -> bool:
        if self.runs is not None:  # done means every slot of the day ran successfully
            return self.settled
        return self.row['status'] == 'complete' or self.settled


@dataclass
class Checklist:
    sections: list  # [(Section, [Item, ...]), ...] in catalog order, empty sections dropped
    orphans: list  # sub-step rows whose parent is not on the board

    @property
    def items(self) -> list:
        return [item for _, items in self.sections for item in items]


@dataclass
class Agenda:
    """The remaining rounds of the shown service day, seen from the snapshot's clock."""

    now: datetime
    day: date
    reset: datetime
    runs: list  # [datetime] remaining wakes before the reset, sorted
    labels: dict  # {datetime: label} for server-observed wakes
    first_tomorrow: datetime
    in_round: bool = False  # a round is executing right now
    pending: frozenset = frozenset()  # sections whose phase has not started this round
    active: frozenset = frozenset()  # sections whose phase is running
    regular: list = field(default_factory=list)  # runs that are not one phase's slot only
    dynamic_only: frozenset = frozenset()  # server-reported runs (not a fixed time)
    dynamic_sections: frozenset | None = None  # what those runs execute (None: everything)


def _font(text: str, color: str) -> str:
    return f"<font color='{color}'>{text}</font>"


def _clean(text: str) -> str:
    # Titles and reasons come from fixed catalogs; keep markup out regardless.
    return ''.join(ch for ch in str(text) if ch not in '<>*~`[]')


def _number(value) -> bool:
    return type(value) is int and value >= 0


class CardRenderer:
    """Render a projection snapshot as a Lark card for one project's catalog."""

    def __init__(self, catalog: Catalog, schedule: Schedule):
        self.catalog = catalog
        self.schedule = schedule

    # -- grouping --------------------------------------------------------

    def slotted(self) -> dict:
        """``{task key: phase}`` of the tasks whose phase runs at its own slots."""
        result = {}
        for phase in self.schedule.slot_map:
            section = self.catalog.phase_sections.get(phase, phase)
            for key, spec in self.catalog.tasks.items():
                if spec.section == section and spec.kind != 'step':
                    result[key] = phase
        return result

    def is_problem(self, row: dict) -> bool:
        """A real failure, or a request that was sent without a returned result."""
        return row.get('status') == 'failed' or row.get('reason') in self.catalog.request_problems

    def checklist(self, snapshot: dict) -> Checklist:
        """Group observations below their task; missing sub-step data is not promoted."""
        catalog = self.catalog
        rows = {}
        raw = snapshot.get('tasks')
        for row in raw if type(raw) is list else ():
            if (
                type(row) is dict
                and type(row.get('task_id')) is str
                and row['task_id'] in catalog.tasks
                and row.get('status') in STATUSES
                and row['status'] != 'closed'
                and row.get('availability') != 'closed'
            ):
                rows[row['task_id']] = dict(row)
        with_jobs = {catalog.tasks[key].section for key in rows if catalog.tasks[key].kind == 'job'}
        replaced = {
            section.aggregate
            for section in catalog.sections
            if section.aggregate and section.key in with_jobs
        }
        primary = {
            key: Item(key, row)
            for key, row in rows.items()
            if catalog.tasks[key].kind != 'step' and key not in replaced
        }
        orphans = []
        for key, row in rows.items():
            spec = catalog.tasks[key]
            if spec.kind != 'step':
                continue
            parent = row.get('parent_task')
            if parent not in catalog.tasks or catalog.tasks[parent].kind == 'step':
                parent = spec.parent
            if parent in primary:
                primary[parent].children.append(row)
            elif row['status'] == 'running' or self.is_problem(row):
                # An active or failed sub-step without a visible parent is
                # real work; ordinary missing observations go to the details.
                primary[key] = Item(key, row)
            elif row['status'] != 'complete':
                orphans.append(row)
        for item in primary.values():
            item.children.sort(key=lambda child: catalog.order[child['task_id']])
            item.issues = [child for child in item.children if self.is_problem(child)]
            item.active = next(
                (child for child in item.children if child['status'] == 'running'), None
            )
            item.settled = (
                item.row['status'] in SETTLED_STATES
                and item.row.get('reason') in catalog.day_done_reasons
                and item.active is None
            )
        saved = snapshot.get('slot_runs')
        saved = saved if type(saved) is dict else {}
        for key, phase in self.slotted().items():
            item = primary.get(key)
            if item is None:
                continue
            target = len(self.schedule.slot_map[phase])
            indexes = saved.get(phase)
            item.slot_done = frozenset(
                index
                for index in (indexes if type(indexes) is list else ())
                if type(index) is int and 0 <= index < target
            )
            item.runs = (len(item.slot_done), target)
            item.settled = (
                item.runs[0] >= target
                and item.status not in ('failed', 'running')
                and item.active is None
            )
        sections = []
        for section in catalog.sections:
            items = sorted(
                (
                    item
                    for item in primary.values()
                    if catalog.tasks[item.key].section == section.key
                ),
                key=lambda item: catalog.order[item.key],
            )
            if items:
                sections.append((section, items))
        orphans.sort(key=lambda row: catalog.order[row['task_id']])
        return Checklist(sections, orphans)

    # -- time ----------------------------------------------------------------

    def _parse(self, value) -> datetime | None:
        if type(value) is not str or len(value) > 40:
            return None
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            return None
        return stamp.astimezone(self.schedule.tz) if stamp.tzinfo is not None else None

    def _time(self, value, day: str) -> str | None:
        """``HH:MM`` within the shown service day, otherwise ``MM-DD HH:MM``."""
        stamp = self._parse(value)
        if stamp is None:
            return None
        same_day = self.schedule.service_day(stamp) == day
        return stamp.strftime('%H:%M') if same_day else stamp.strftime('%m-%d %H:%M')

    def agenda(self, snapshot: dict, checklist: Checklist) -> Agenda:
        schedule = self.schedule
        day = date.fromisoformat(snapshot['service_day'])
        start = schedule.day_start(day.isoformat())  # the day's reset
        now = self._parse(snapshot.get('observed_at')) or start
        now = max(now, start)
        reset = start + timedelta(days=1)
        fixed = set()
        for text in schedule.times:
            stamp = schedule.on_day(day.isoformat(), text)
            if now < stamp < reset:
                fixed.add(stamp)
        slotted = self.slotted()
        labels = {}
        for phase in schedule.slot_map:
            title = next(
                (self.catalog.title(key) for key, owner in slotted.items() if owner == phase), ''
            )
            for stamp in schedule.slot_stamps(phase, day.isoformat()):
                if now < stamp < reset and not any(abs(stamp - run) < SAME_TIME for run in fixed):
                    labels[stamp] = title  # this wake runs only that phase
        dynamic_sections = (
            None
            if schedule.dynamic_phases is None
            else frozenset(self.catalog.phase_sections.get(p, p) for p in schedule.dynamic_phases)
        )
        fallback = '、'.join(
            section.title
            for section in self.catalog.sections
            if dynamic_sections and section.key in dynamic_sections
        )
        dynamic = set()
        wakes = snapshot.get('dynamic_candidates')
        for value in [*(wakes if type(wakes) is list else ()), snapshot.get('next_check_at')]:
            stamp = self._parse(value)
            if stamp is None or not now < stamp < reset or schedule.quiet(stamp):
                continue
            if any(abs(stamp - run) < SAME_TIME for run in fixed):
                continue
            same = next((run for run in labels if abs(stamp - run) < SAME_TIME), None)
            if same is not None:
                if schedule.is_dynamic(stamp, wakes if type(wakes) is list else ()):
                    dynamic.add(same)  # a slot wake that also runs the server's phases
                continue
            dynamic.add(stamp)
            titles = [
                self.catalog.title(item.key)
                for item in checklist.items
                if not item.done
                and item.key not in slotted
                and (
                    dynamic_sections is None
                    or self.catalog.tasks[item.key].section in dynamic_sections
                )
                and (due := self._parse(item.row.get('next_run_at'))) is not None
                and abs(due - stamp) < SAME_TIME
            ]
            labels[stamp] = titles[0] if titles else fallback
        runs = fixed | set(labels) | dynamic
        regular = sorted(fixed | dynamic)
        tomorrow = (day + timedelta(days=1)).isoformat()
        first = min(schedule.on_day(tomorrow, text) for text in schedule.times)
        return Agenda(
            now, day, reset, sorted(runs), labels, first, regular=regular,
            dynamic_only=frozenset(dynamic), dynamic_sections=dynamic_sections,
        )  # fmt: skip

    @staticmethod
    def _when(stamp: datetime, agenda: Agenda) -> str:
        """Compact time: 13:15 / 凌晨01:15 / 次日08:00 / 昨晚23:30 / 明日06:00.

        Plain times are on the date the shown day is named after; ``明日`` is
        the next service day; the others are the edges of a day that spans
        two dates.
        """
        clock = stamp.strftime('%H:%M')
        if stamp >= agenda.reset:
            return '明日' + clock
        if stamp.date() == agenda.day:
            return clock
        if stamp.date() > agenda.day:
            return ('凌晨' if stamp.hour < 6 else '次日') + clock
        return '昨晚' + clock

    def _next_step(self, item: Item, agenda: Agenda) -> str | None:
        """When an unfinished item will be executed next (a ``明日`` time means not today)."""
        state, row = item.status, item.row
        if state == 'running' or item.active is not None:
            return None
        if state == 'unsupported' or row.get('reason') == 'window_ended':
            return '今日不执行'
        tomorrow = self._when(agenda.first_tomorrow, agenda)
        verb = VERBS.get(state, '执行')
        due = self._parse(row.get('next_run_at'))
        section = self.catalog.tasks[item.key].section
        later = agenda.in_round and (
            section in agenda.pending or (section in agenda.active and not row.get('seen'))
        )
        if later and (due is None or due <= agenda.now):
            return '本轮稍后'
        phase = self.slotted().get(item.key)
        if phase is not None:
            return self._next_slot(item, phase, agenda)
        runs = [
            stamp
            for stamp in agenda.regular
            if stamp not in agenda.dynamic_only
            or agenda.dynamic_sections is None
            or section in agenda.dynamic_sections
        ]
        if due is not None and due > agenda.now:
            if due >= agenda.reset:
                return f'{self._when(due, agenda)}到期'
            attempt = next((run for run in runs if run >= due - SAME_TIME), None)
            if attempt is None:
                return f'{self._when(due, agenda)}到期 · {tomorrow}执行'
            if abs(attempt - due) < SAME_TIME:
                return f'{self._when(due, agenda)}执行'
            return f'{self._when(due, agenda)}到期 · {self._when(attempt, agenda)}执行'
        if runs:
            return f'{self._when(runs[0], agenda)}{verb}'
        return f'{tomorrow}{verb}'

    def _next_slot(self, item: Item, phase: str, agenda: Agenda) -> str:
        """A slotted task runs at its next slot, or at the next wake to catch up."""
        schedule = self.schedule
        window = schedule.slot_window(phase, agenda.now)
        if window is not None and window not in item.slot_done:
            verb = '重试' if item.status == 'failed' else '执行'
            when = agenda.runs[0] if agenda.runs else agenda.first_tomorrow
            return f'{self._when(when, agenda)}{verb}'
        upcoming = [
            s for s in schedule.slot_stamps(phase, agenda.day.isoformat()) if s > agenda.now
        ]
        if not upcoming:
            upcoming = schedule.slot_stamps(phase, (agenda.day + timedelta(days=1)).isoformat())
        return f'{self._when(upcoming[0], agenda)}执行'

    # -- text helpers ------------------------------------------------------

    def _progress(self, row: dict) -> str:
        current, total = row.get('current'), row.get('total')
        unit = row.get('unit') if row.get('unit') in self.catalog.units else ''
        if _number(current) and _number(total) and total > 0:
            return f'{current}/{total}'
        if _number(current):
            return f'{current} {unit}'.rstrip()
        return ''

    def _reason_text(self, row: dict) -> str:
        reasons = self.catalog.reasons
        return reasons.get(row.get('reason')) or reasons['failure']

    def _label(self, item: Item) -> tuple[str, str] | None:
        """Short inline status after an unfinished title: ``(text, colour)``."""
        row, state = item.row, item.status
        progress = self._progress(row)
        if item.active is not None:
            child = item.active
            child_progress = self._progress(child)
            text = '执行中 · ' + self.catalog.title(child['task_id'])
            return (text + (' ' + child_progress if child_progress else ''), 'blue')
        if state == 'running':
            return ('执行中' + (' · ' + progress if progress else ''), 'blue')
        if state == 'failed':
            return ('失败', 'red')
        if state == 'result_unavailable' and row.get('reason') in self.catalog.request_problems:
            return ('结果未返回', 'orange')
        if item.runs is not None:
            return (f'{item.runs[0]}/{item.runs[1]}', 'grey')
        if state == 'result_unavailable':
            return ('状态未返回', 'grey')
        if state == 'unsupported':
            return ('暂无流程', 'orange')
        if row.get('reason') in self.catalog.short_reasons:
            color = 'orange' if state == 'blocked' else 'grey'
            return (self.catalog.short_reasons[row['reason']], color)
        if state == 'blocked':
            return ('暂不满足', 'grey')
        if progress:
            return (progress, 'grey')
        return ('等待', 'grey') if state == 'waiting' else None

    def _why(self, item: Item) -> str | None:
        """The long reason of an unfinished item, for the details panel."""
        row, state = item.row, item.status
        if state in ('running', 'complete') or item.active is not None:
            return None
        text = self.catalog.reasons.get(row.get('reason'))
        if row.get('reason') == 'completed_observation_retained':
            text = None
        if text is None and state in ('failed', 'blocked', 'result_unavailable', 'unsupported'):
            text = STATE_TEXT[state]
        return text

    # -- elements --------------------------------------------------------------

    @staticmethod
    def _markdown(content: str, *, small: bool = False, margin: str = '0px') -> dict:
        return {
            'tag': 'markdown',
            'content': content,
            'text_size': 'notation' if small else 'normal',
            'margin': margin,
        }

    @staticmethod
    def _marked(content: str, icon: dict) -> dict:
        """One task line led by a read-only status icon."""
        return {
            'tag': 'div',
            'text': {'tag': 'lark_md', 'content': content},
            'icon': dict(icon),
            'margin': '0px',
        }

    @staticmethod
    def _panel(ident: str, header: str, elements: list, *, expanded: bool) -> dict:
        return {
            'tag': 'collapsible_panel',
            'element_id': ident[:20],
            'expanded': expanded,
            'padding': '0px 0px 4px 0px',
            'vertical_spacing': '6px',
            'header': {
                'title': {'tag': 'markdown', 'content': header},
                'vertical_align': 'center',
                # Lark rejects the two-value form here ("invalid panel header padding").
                'padding': '4px 0px 4px 0px',
                'icon': {
                    'tag': 'standard_icon',
                    'token': 'down-small-ccm_outlined',
                    'color': 'grey',
                    'size': '16px 16px',
                },
                'icon_position': 'right',
                'icon_expanded_angle': -180,
            },
            'elements': elements,
        }

    def _item_elements(self, item: Item, agenda: Agenda, compact: bool) -> list:
        """One line per item: title, short status and when it runs next."""
        title = _clean(self.catalog.title(item.key))
        if item.done:
            done = _font('~~' + title + '~~', 'grey')
            return [self._markdown('✓ ' + done)] if compact else [self._marked(done, DONE_ICON)]
        running = item.status == 'running' or item.active is not None
        if running:
            title = f'**{title}**'
        label = self._label(item)
        nxt = self._next_step(item, agenda)
        parts = []
        if label and label[0]:
            parts.append(_font(_clean(label[0]), label[1]))
        if nxt:
            parts.append(_font(_clean(nxt), 'grey'))
        text = title + ('　' + _font(' · ', 'grey').join(parts) if parts else '')
        failed = item.status == 'failed'
        unknown = (
            item.status == 'result_unavailable'
            and item.row.get('reason') in self.catalog.request_problems
        )
        if compact:
            mark = '✗ ' if failed else '! ' if unknown else '◉ ' if running else '○ '
            return [self._markdown(mark + text)]
        icon = (
            FAILED_ICON if failed else UNKNOWN_ICON if unknown
            else RUNNING_ICON if running else PENDING_ICON
        )  # fmt: skip
        return [self._marked(text, icon)]

    def _phase_rows(self, snapshot: dict) -> dict:
        rows = snapshot.get('phases')
        return {
            row['phase']: row
            for row in (rows if type(rows) is list else ())
            if type(row) is dict and row.get('phase') in self.catalog.phases
        }

    def _section_element(self, section, items, phases, agenda, compact) -> dict:
        catalog = self.catalog
        done = [item for item in items if item.done]
        rows = []
        for item in items:
            if not item.done:
                rows.append(item)
            # A failing sub-step is its own line, even under a finished parent.
            rows += [Item(child['task_id'], child) for child in item.issues]
        problems = sum(1 for item in items if self.is_problem(item.row) or item.issues)
        section_phases = [
            row for phase, row in phases.items() if catalog.phase_sections.get(phase) == section.key
        ]
        failed_phases = [
            row for row in section_phases if row.get('status') in ('failed', 'result_unavailable')
        ]
        running = any(row.get('status') == 'running' for row in section_phases) or any(
            item.status == 'running' or item.active is not None for item in items
        )
        header = f'**{_clean(section.title)}**　' + _font(f'{len(done)}/{len(items)}', 'grey')
        if running:
            header += '　' + _font('执行中', 'blue')
        elif problems:
            header += '　' + _font(f'{problems} 项异常', 'red')
        elif failed_phases:
            header += '　' + _font('阶段异常', 'red')
        elif not rows:
            header += '　' + _font('✓', 'green')
        elements = [
            element for item in rows for element in self._item_elements(item, agenda, compact)
        ]
        for row in failed_phases:
            reason = catalog.reasons.get(row.get('reason'), '该阶段未返回成功结果')
            title = catalog.phases[row['phase']]
            elements.append(
                self._markdown(_font(_clean(f'{title}阶段 · {reason}'), 'red'), small=True)
            )
        finished = [
            element for item in done for element in self._item_elements(item, agenda, compact)
        ]
        if rows or failed_phases:
            if finished:
                elements.append(
                    self._panel(
                        f'done_{section.key}',
                        _font(f'已完成 {len(done)} 项', 'grey'),
                        finished,
                        expanded=False,
                    )
                )
        else:
            elements = finished
        settled = not rows and not failed_phases and not running
        return self._panel(f'sec_{section.key}', header, elements, expanded=not settled)

    def _plan_element(self, agenda: Agenda, running_text: str | None) -> dict:
        reset = agenda.reset.strftime('%H:%M')
        if agenda.runs:
            head = f'今日还剩 {len(agenda.runs)} 轮 · 下一轮 {self._when(agenda.runs[0], agenda)}'
            chips = []
            for index, run in enumerate(agenda.runs):
                text = self._when(run, agenda) + agenda.labels.get(run, '')
                chips.append(_clean(text + ('末轮' if index == len(agenda.runs) - 1 else '')))
            chips.append(f'{reset}新一天')
        else:
            head = f'今日轮次已结束 · 下一轮 {self._when(agenda.first_tomorrow, agenda)}'
            chips = [f'{reset}新一天']
        lines = ['**执行计划**　' + _font(head, 'grey')]
        if running_text:
            lines.append(_font(_clean(running_text), 'blue'))
        lines.append(_font(' · '.join(chips), 'grey'))
        return self._markdown('\n'.join(lines))

    def _details(self, checklist: Checklist) -> dict | None:
        """Reasons of unfinished items and sub-step observations, on demand."""
        catalog = self.catalog

        def describe(row):
            return catalog.reasons.get(row.get('reason')) or STATE_TEXT.get(row['status'], '尚未取得状态')

        reasons = []
        for item in checklist.items:
            for entry in ([] if item.done else [item]) + [
                Item(c['task_id'], c) for c in item.issues
            ]:
                why = self._why(entry)
                if why:
                    reasons.append(catalog.title(entry.key) + '：' + why)
        blocks = [('原因', reasons)] if reasons else []
        for item in checklist.items:
            lines = [
                catalog.title(child['task_id']) + ' · ' + describe(child)
                for child in item.children
                if child['status'] not in ('complete', 'running') and not self.is_problem(child)
            ]
            if lines:
                blocks.append((catalog.title(item.key), lines))
        if checklist.orphans:
            blocks.append(
                (
                    '其他',
                    [
                        catalog.title(row['task_id']) + ' · ' + describe(row)
                        for row in checklist.orphans
                    ],
                )
            )
        if not blocks:
            return None
        elements = [
            self._markdown(
                f'**{_clean(title)}**\n'
                + '\n'.join(_font(_clean(line), 'grey') for line in dict.fromkeys(lines)),
                small=True,
            )
            for title, lines in blocks
        ]
        return self._panel('task_details', _font('详情', 'grey'), elements, expanded=False)

    # -- card ----------------------------------------------------------------

    def render(self, snapshot) -> dict:
        source = snapshot if type(snapshot) is dict else {}
        card = self._render(source, details=True, compact=False)
        if self._size(card) > CARD_LIMIT:
            card = self._render(source, details=False, compact=False)
        if self._size(card) > CARD_LIMIT:
            # Keep every item and every failure; only the layout gets denser.
            card = self._render(source, details=False, compact=True)
        return card

    @staticmethod
    def _size(card: dict) -> int:
        return len(json.dumps(card, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))

    def _render(self, source: dict, *, details: bool, compact: bool) -> dict:
        catalog = self.catalog
        try:
            day = date.fromisoformat(source.get('service_day', ''))
        except (TypeError, ValueError):
            raise ValueError('invalid board day') from None
        day_text = day.isoformat()
        checklist = self.checklist(source)
        agenda = self.agenda(source, checklist)
        items = checklist.items
        phases = self._phase_rows(source)
        order = [p for p in source.get('round_phases') or () if p in catalog.phases]
        if order and not source.get('finished_at'):
            agenda.in_round = True
            agenda.pending = frozenset(
                catalog.phase_sections[p]
                for p in order
                if p not in phases and p in catalog.phase_sections
            )
            agenda.active = frozenset(
                catalog.phase_sections[p]
                for p, row in phases.items()
                if row.get('status') == 'running' and p in catalog.phase_sections
            )
        done = sum(item.done for item in items)
        total = len(items)
        failed_phases = [
            row for row in phases.values() if row.get('status') in ('failed', 'result_unavailable')
        ]
        problems = sum(1 for item in items if self.is_problem(item.row) or item.issues)
        problems += len(failed_phases)
        active_items = [
            item for item in items if item.status == 'running' or item.active is not None
        ]
        running_phases = [row for row in phases.values() if row.get('status') == 'running']
        running = bool(source.get('running')) or bool(running_phases) or bool(active_items)
        running_text = None
        if running:
            phase = running_phases[-1] if running_phases else None
            names = [catalog.title((item.active or item.row)['task_id']) for item in active_items]
            parts = ([catalog.phases[phase['phase']] + '阶段'] if phase else []) + names[:2]
            running_text = '正在执行：' + ' · '.join(parts) if parts else '正在执行'

        elements = []
        status_rows = [
            row
            for phase, row in phases.items()
            if catalog.phase_sections.get(phase) is None
            and row.get('status') in ('failed', 'result_unavailable')
        ]
        for row in status_rows:
            reason = catalog.reasons.get(row.get('reason'), '该阶段未返回成功结果')
            elements.append(self._markdown(_font(_clean(f'本轮未能读取完整计划 · {reason}'), 'red')))
        if not status_rows and not source.get('discovered'):
            text = '本轮未能读取完整计划' if source.get('finished_at') else '正在读取今日任务…'
            elements.append(self._markdown(_font(text, 'grey'), small=True))
        elements.append(self._plan_element(agenda, running_text))
        for section, section_items in checklist.sections:
            elements.append(self._section_element(section, section_items, phases, agenda, compact))
        if details and (panel := self._details(checklist)) is not None:
            elements.append(panel)
        if observed := self._time(source.get('observed_at'), day_text):
            elements.append(self._markdown(_font('更新 ' + observed, 'grey'), small=True))

        tags = []
        if running:
            template = 'blue'
            tags.append(('执行中', 'blue'))
        elif total and done == total and not problems:
            template = 'green'
            tags.append(('全部完成', 'green'))
        elif problems:
            template = 'orange'
        elif not source.get('discovered'):
            template = 'grey'
            tags.append(('读取中', 'neutral'))
        else:
            template = 'wathet'
            tags.append(
                (
                    ('下次 ' + self._when(agenda.runs[0], agenda).replace('今日 ', ''))
                    if agenda.runs
                    else '今日已结束',
                    'wathet',
                )
            )
        if problems:
            tags.append((f'{problems} 项异常', 'red'))

        summary = f'{catalog.display_name} 每日清单 {done}/{total}'
        if running:
            summary += ' · 执行中'
        if problems:
            summary += f' · {problems} 项异常'
        title = f'{catalog.display_name} · {day.month}月{day.day}日 周{WEEKDAYS[day.weekday()]}'
        return {
            'schema': '2.0',
            'config': {'update_multi': True, 'summary': {'content': summary}},
            'header': {
                'template': template,
                'title': {'tag': 'plain_text', 'content': title},
                'subtitle': {'tag': 'plain_text', 'content': f'每日清单 · 已完成 {done}/{total}'},
                'text_tag_list': [
                    {
                        'tag': 'text_tag',
                        'text': {'tag': 'plain_text', 'content': text},
                        'color': color,
                    }
                    for text, color in tags[:3]
                ],
                'padding': '12px 16px 10px 16px',
            },
            'body': {
                'direction': 'vertical',
                'padding': '8px 16px 12px 16px',
                'vertical_spacing': '8px',
                'elements': elements,
            },
        }
