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

from .catalog import DONE_FIELD, ERROR_CODE_UNITS, STATUSES, Catalog, idle_phases
from ..schedule import Schedule

CARD_LIMIT = 28 * 1024
# Lark card JSON 2.0 takes at most 200 elements or components per card, and
# every node with a tag counts (text, icons, panels). Keep a margin below it.
TAG_LIMIT = 190
WEEKDAYS = '一二三四五六日'
# Read-only status icons (a todolist look without a clickable box), all from the
# library's outlined ring family: ○ pending, a spinning ring while running, a
# ringed tick when done, a ringed ! on failure, a ringed ? when a request's
# result never came back. The spinner is an uploaded animation (``icons``);
# without it the static loading ring stands in.
PENDING_ICON = {'tag': 'standard_icon', 'token': 'ellipse_outlined', 'color': 'grey'}
RUNNING_ICON = {'tag': 'standard_icon', 'token': 'loading_outlined', 'color': 'blue'}
DONE_ICON = {'tag': 'standard_icon', 'token': 'yes_outlined', 'color': 'green'}
FAILED_ICON = {'tag': 'standard_icon', 'token': 'warning_outlined', 'color': 'red'}
UNKNOWN_ICON = {'tag': 'standard_icon', 'token': 'maybe_outlined', 'color': 'orange'}
# A waiting item of a recurring phase (one that runs at its own times).
RECURRING_ICON = {'tag': 'standard_icon', 'token': 'time_outlined', 'color': 'grey'}
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
# Notes on a finished item that did not get done the ordinary way.
DONE_NOTES = {'retry': '重试后完成', 'confirm': '核对后确认', 'manual': '补跑完成'}
SAME_TIME = timedelta(seconds=120)
# A task failing this many rounds in a row is called out on the card and in
# the chat-list preview (a single failure already shows red on its row).
STREAK_ALERT = 3
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
    slot_only: frozenset = frozenset()  # wakes that run only a recurring phase's slot
    owned: dict = field(default_factory=dict)  # {server time: sections} only wake phases run


def _font(text: str, color: str) -> str:
    return f"<font color='{color}'>{text}</font>"


def _clean(text: str) -> str:
    # Titles and reasons come from fixed catalogs; keep markup out regardless.
    return ''.join(ch for ch in str(text) if ch not in '<>*~`[]')


def _number(value) -> bool:
    return type(value) is int and value >= 0


def _titles(titles: list) -> str:
    """Name a few items in full; a longer list as its first three and a count."""
    if len(titles) <= 3:
        return '、'.join(titles)
    return '、'.join(titles[:3]) + f' 等 {len(titles)} 项'


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

    def recurring(self) -> dict:
        """``{section key: phase}`` of the phases that run at their own times."""
        return {self.catalog.phase_sections.get(p, p): p for p in self.schedule.slot_map}

    @staticmethod
    def off_today(item) -> bool:
        """Not done, and nothing more will happen today: a missed window, or no flow."""
        return not item.done and (
            item.status == 'unsupported' or item.row.get('reason') == 'window_ended'
        )

    def is_problem(self, row: dict) -> bool:
        """A real failure, or a request that was sent without a returned result."""
        return row.get('status') == 'failed' or row.get('reason') in self.catalog.request_problems

    def checklist(self, snapshot: dict) -> Checklist:
        """Group observations below their task; missing sub-step data is not promoted."""
        catalog = self.catalog
        try:  # the game's next day starts here (schedule.reset)
            day_end = self.schedule.day_start(
                date.fromisoformat(snapshot['service_day']).isoformat()
            ) + timedelta(days=1)
        except (KeyError, TypeError, ValueError):
            day_end = None
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
            later = self._parse(item.row.get('next_run_at'))
            item.settled = (
                item.row['status'] in SETTLED_STATES
                and item.active is None
                and (
                    item.row.get('reason') in catalog.day_done_reasons
                    # Nothing more can happen before the game's day turns over.
                    or (day_end is not None and later is not None and later >= day_end)
                )
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
            if phase in self.schedule.wake_phases:
                # It says itself when its tasks are done; its runs head the section.
                continue
            # A slot-only phase is done for today once every slot ran (e.g. the
            # three dispatch windows of 事件簿).
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
        slot_only = frozenset(labels)
        wake = set(schedule.wake_phases)
        claims = []
        saved = snapshot.get('dynamic_owners')
        for value, found in saved.items() if type(saved) is dict else ():
            stamp = self._parse(value)
            if stamp is not None and type(found) is list and found and set(found) <= wake:
                claims.append(
                    (stamp, frozenset(self.catalog.phase_sections.get(p, p) for p in found))
                )
        owned = {}
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
            claim = next((found for when, found in claims if abs(when - stamp) < SAME_TIME), None)
            if claim is not None:
                # Only the recurring phase that reported this time runs then.
                if same is None:
                    owned[stamp] = claim
                    titles = [
                        self.catalog.title(item.key)
                        for item in checklist.items
                        if self.catalog.run_section(item.key) in claim
                        and (due := self._parse(item.row.get('next_run_at'))) is not None
                        and abs(due - stamp) < SAME_TIME
                    ]
                    labels[stamp] = titles[0] if titles else ''
                continue
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
                    or self.catalog.run_section(item.key) in dynamic_sections
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
            slot_only=slot_only, owned=owned,
        )  # fmt: skip

    @staticmethod
    def _when(stamp: datetime, agenda: Agenda) -> str:
        """Compact time: 13:15 / 凌晨01:15 / 次日08:00 / 昨晚23:30 / 明日06:00 / 今晚23:31.

        Plain times are on the date the shown day is named after; ``明日`` is
        the next service day; the others are the edges of a day that spans
        two dates. A day named after the date it ends on starts the evening
        before: a time still to come that evening is ``今晚`` to whoever reads
        the card then (``昨晚``/``明日`` would point a day off before midnight).
        """
        clock = stamp.strftime('%H:%M')
        if (
            agenda.reset.date() == agenda.day  # the day began the evening before its date
            and agenda.now < stamp
            and stamp.date() == agenda.now.astimezone(stamp.tzinfo).date()
            and (stamp.date() < agenda.day or stamp >= agenda.reset)
        ):
            return '今晚' + clock
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
        section = self.catalog.run_section(item.key)
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
        own = self.catalog.tasks[item.key].phase
        if own in self.schedule.slot_map:
            # Listed under another section but run by a phase with its own times.
            runs = sorted({*runs, *self._phase_runs(own, agenda)})
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
        if schedule.slot_due(phase, agenda.now, item.slot_done):
            verb = '重试' if item.status == 'failed' else '执行'
            when = agenda.runs[0] if agenda.runs else agenda.first_tomorrow
            return f'{self._when(when, agenda)}{verb}'
        return f'{self._when(self._next_run(phase, agenda), agenda)}执行'

    def _phase_runs(self, phase: str, agenda: Agenda) -> list:
        """Today's remaining wakes of a recurring phase: its slots and times it reported."""
        section = self.catalog.phase_sections.get(phase, phase)
        upcoming = [
            s for s in self.schedule.slot_stamps(phase, agenda.day.isoformat()) if s > agenda.now
        ]
        return sorted(upcoming + [s for s, found in agenda.owned.items() if section in found])

    def _next_run(self, phase: str, agenda: Agenda) -> datetime:
        """A recurring phase's next run: its next slot, or an earlier time it reported."""
        schedule = self.schedule
        upcoming = self._phase_runs(phase, agenda)
        if not upcoming:
            upcoming = schedule.slot_stamps(phase, (agenda.day + timedelta(days=1)).isoformat())
        return min(upcoming)

    # -- text helpers ------------------------------------------------------

    def _progress(self, row: dict) -> str:
        current, total = row.get('current'), row.get('total')
        unit = row.get('unit') if row.get('unit') in self.catalog.units else ''
        if _number(current) and _number(total) and total > 0:
            return f'{current}/{total}'
        if _number(current):
            return f'{current} {unit}'.rstrip()
        return ''

    def _done_note(self, item: Item, agenda: Agenda) -> str:
        """When a finished item got done; how, only when it was not the ordinary way."""
        row = item.row
        parts = []
        stamp = self._parse(row.get('done_at'))
        if stamp is not None and agenda.reset - timedelta(days=1) <= stamp < agenda.reset:
            parts.append(_font(self._when(stamp, agenda), 'grey'))
        filled = self._filled(self.catalog.tasks[item.key].done_text, row)
        if filled:
            parts.append(_font(filled, 'grey'))
        note = DONE_NOTES.get(row.get('done_how'))
        if item.settled and item.runs is None:
            # Today's part is over without the target being reached: say why
            # (the first clause of the reason, e.g. 今日次数已达上限).
            why = self.catalog.reasons.get(row.get('reason'), '').split('，')[0] or '今日已无可做'
            if row.get('reason') not in self.catalog.day_done_reasons:
                why += ' · 换日后继续'  # waits for the game's next day
            progress = self._progress(row)
            note = why + (' ' + progress if progress else '')
        if note:
            parts.append(_font(note, 'orange'))
        return '　' + _font(' · ', 'grey').join(parts) if parts else ''

    def _filled(self, template, row: dict) -> str:
        """A manifest row text (``done_text``/``progress_text``) filled from counts.

        Empty unless every field it names was reported as a whole number, so
        the row then looks as it would without the text.
        """
        counts = row.get('counts')
        if not template or type(counts) is not dict:
            return ''
        if not all(_number(counts.get(name)) for name in DONE_FIELD.findall(template)):
            return ''
        return _clean(DONE_FIELD.sub(lambda field: str(counts[field.group(1)]), template))

    def _reason_text(self, row: dict) -> str:
        reasons = self.catalog.reasons
        return reasons.get(row.get('reason')) or reasons['failure']

    def _label(self, item: Item) -> tuple[str, str] | None:
        """Short inline status after an unfinished title: ``(text, colour)``."""
        row, state = item.row, item.status
        progress = self._progress(row)
        own = self._filled(self.catalog.tasks[item.key].progress_text, row)
        if item.active is not None:
            child = item.active
            child_progress = self._progress(child)
            text = '执行中 · ' + self.catalog.title(child['task_id'])
            return (text + (' ' + child_progress if child_progress else ''), 'blue')
        if state == 'running':
            shown = own or progress
            return ('执行中' + (' · ' + shown if shown else ''), 'blue')
        if state == 'failed':
            streak = row.get('fail_streak')
            head = f'连续失败 {streak} 轮' if _number(streak) and streak >= 2 else '失败'
            code, unit = row.get('current'), row.get('unit')
            if type(unit) is str and unit in ERROR_CODE_UNITS and unit in self.catalog.units and _number(code):
                sign = '-' if unit == '负错误码' else ''
                return (f'{head} · 错误码 {sign}{code}', 'red')
            return (head, 'red')
        if state == 'result_unavailable' and row.get('reason') in self.catalog.request_problems:
            return ('结果未返回', 'orange')
        if item.runs is not None:
            return None  # its runs head the section; dispatch counts are not progress
        if state == 'result_unavailable':
            return ('状态未返回', 'grey')
        if state == 'unsupported':
            return ('暂无流程', 'orange')
        if own:  # the manifest's own words replace the short reason and progress
            return (own, 'orange' if state == 'blocked' else 'grey')
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

    def _item_elements(
        self, item: Item, agenda: Agenda, compact: bool, running_icon=None, folded=False
    ) -> list:
        """One line per item: title, short status and when it runs next.

        ``folded`` rows sit under 「今日不执行」, so they do not repeat it.
        """
        title = _clean(self.catalog.title(item.key))
        if item.done:
            done = _font('~~' + title + '~~', 'grey') + self._done_note(item, agenda)
            return [self._markdown('✓ ' + done)] if compact else [self._marked(done, DONE_ICON)]
        running = item.status == 'running' or item.active is not None
        if running:
            title = f'**{title}**'
        label = self._label(item)
        nxt = None if folded else self._next_step(item, agenda)
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
        recurring = self.catalog.tasks[item.key].section in self.recurring()
        if compact:
            idle = '– ' if folded else '◷ ' if recurring else '○ '
            mark = '! ' if failed else '? ' if unknown else '◉ ' if running else idle
            return [self._markdown(mark + text)]
        idle = RECURRING_ICON if recurring else PENDING_ICON
        icon = (
            FAILED_ICON if failed else UNKNOWN_ICON if unknown
            else running_icon or RUNNING_ICON if running else idle
        )  # fmt: skip
        return [self._marked(text, icon)]

    def _phase_rows(self, snapshot: dict) -> dict:
        rows = snapshot.get('phases')
        return {
            row['phase']: row
            for row in (rows if type(rows) is list else ())
            if type(row) is dict and row.get('phase') in self.catalog.phases
        }

    def _runs(self, phase: str, source: dict, agenda: Agenda) -> tuple:
        """``(successful windows, slots, strip, still due, missed)`` of a phase today.

        ``missed``: the latest window had no successful run and will not get one.
        """
        stamps = self.schedule.slot_stamps(phase, agenda.day.isoformat())

        def indexes(key):
            saved = source.get(key)
            values = saved.get(phase) if type(saved) is dict else None
            return {i for i in values or () if type(i) is int and 0 <= i < len(stamps)}

        ran, failed = indexes('slot_runs'), indexes('slot_fails')
        window = self.schedule.slot_window(phase, agenda.now)
        due = self.schedule.slot_due(phase, agenda.now, ran)  # caught up at the next wake
        missed = window is not None and window not in ran and not due  # no retry today
        marks = []
        for index, stamp in enumerate(stamps[: (window + 1) if window is not None else 0]):
            if index in ran:
                mark = _font('✓', 'green')
            elif index in failed:
                mark = _font('✕', 'red')
            elif index == window and due:
                continue  # its run is still due
            else:
                mark = _font('–', 'grey')  # passed without a run
            marks.append(stamp.strftime('%H:%M') + mark)
        strip = ('…  ' if len(marks) > 6 else '') + '  '.join(marks[-6:])
        return len(ran), len(stamps), strip, due, missed

    def _section_element(
        self, section, items, phases, agenda, compact, running_icon=None, runs=None
    ) -> dict:
        catalog = self.catalog
        done = [item for item in items if item.done]
        # Settled for today without being done: folded away, out of the count.
        skipped = [item for item in items if self.off_today(item)]
        rows = []
        for item in items:
            if not item.done and not self.off_today(item):
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
        counted = len(items) - len(skipped)
        count = f'{len(done)}/{counted}' if counted else '今日不执行'
        header = f'**{_clean(section.title)}**　' + _font(count, 'grey')
        if runs is not None:
            count, total, _, upcoming, missed = runs
            header = f'**{_clean(section.title)}**　' + _font(
                f'今日 {count}/{total} 次 · 下次 {self._when(upcoming, agenda)}', 'grey'
            )
            if missed and not running:
                header += '　' + _font('✕', 'red')  # the latest window passed without a run
        if running:
            header += '　' + _font('执行中', 'blue')
        elif problems:
            header += '　' + _font(f'{problems} 项异常', 'red')
        elif failed_phases:
            header += '　' + _font('阶段异常', 'red')
        elif not rows and counted:
            header += '　' + _font('✓', 'green')
        elements = [
            element
            for item in rows
            for element in self._item_elements(item, agenda, compact is True, running_icon)
        ]
        strip_line = []
        if runs is not None and runs[2]:
            strip_line = [self._markdown(_font('今日　', 'grey') + runs[2], small=True)]
            elements = strip_line + elements
        for row in failed_phases:
            reason = catalog.reasons.get(row.get('reason'), '该阶段未返回成功结果')
            title = catalog.phases[row['phase']]
            elements.append(
                self._markdown(_font(_clean(f'{title}阶段 · {reason}'), 'red'), small=True)
            )
        finished = [
            element for item in done for element in self._item_elements(item, agenda, bool(compact))
        ]
        later = [
            element
            for item in skipped
            for element in self._item_elements(item, agenda, bool(compact), folded=True)
        ]
        skip_panel = (
            [
                self._panel(
                    f'skip_{section.key}',
                    _font(f'今日不执行 {len(skipped)} 项', 'grey'),
                    later,
                    expanded=False,
                )
            ]
            if later
            else []
        )
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
            elements += skip_panel
        else:
            elements = strip_line + finished + skip_panel
        settled = not rows and not failed_phases and not running
        return self._panel(f'sec_{section.key}', header, elements, expanded=not settled)

    def _running_line(self, text: str, compact, running_icon=None) -> dict:
        """What the round is doing now, led by the spinner (◉ in the dense layout)."""
        content = _font(_clean(text), 'blue')
        if compact is True:
            line = self._markdown('◉ ' + content)
        else:
            line = self._marked(content, running_icon or RUNNING_ICON)
        return {**line, 'element_id': 'running_now'}

    def _plan_element(self, agenda: Agenda, recurring=()) -> dict:
        """The day's remaining rounds; recurring phases get a line each."""
        reset = agenda.reset.strftime('%H:%M')
        if agenda.runs:
            head = f'今日还剩 {len(agenda.runs)} 轮 · 下一轮 {self._when(agenda.runs[0], agenda)}'
        else:
            head = f'今日轮次已结束 · 下一轮 {self._when(agenda.first_tomorrow, agenda)}'
        daily = [
            run
            for run in agenda.runs
            if not recurring or (run not in agenda.slot_only and run not in agenda.owned)
        ]
        chips = []
        for index, run in enumerate(daily):
            label = '' if recurring else agenda.labels.get(run, '')
            text = self._when(run, agenda) + label + ('末轮' if index == len(daily) - 1 else '')
            chips.append(_clean(text))
        chips.append(f'{reset}新一天')
        lines = ['**执行计划**　' + _font(head, 'grey')]
        for phase in recurring:
            lines.append(_font(_clean('循环 · ' + self._cadence(phase, agenda)), 'grey'))
        lines.append(_font(('每日：' if recurring else '') + ' · '.join(chips), 'grey'))
        return self._markdown('\n'.join(lines))

    def _cadence(self, phase: str, agenda: Agenda) -> str:
        """``指挥官中心：每 2 小时，下次 12:00，末次 22:00`` or its times listed."""
        stamps = self.schedule.slot_stamps(phase, agenda.day.isoformat())
        gaps = {(b - a) for a, b in zip(stamps, stamps[1:])}
        upcoming = self._when(self._next_run(phase, agenda), agenda)
        title = self.catalog.phases.get(phase, phase)
        if (
            len(stamps) > 4
            and len(gaps) == 1
            and (gap := gaps.pop()) % timedelta(hours=1) == timedelta()
        ):
            last = self._when(stamps[-1], agenda)
            return f'{title}：每 {gap // timedelta(hours=1)} 小时，下次 {upcoming}，末次 {last}'
        times = ' · '.join(self._when(stamp, agenda) for stamp in stamps)
        return f'{title}：{times}，下次 {upcoming}'

    def _details(self, checklist: Checklist) -> dict | None:
        """Reasons of unfinished items and sub-step observations, on demand."""
        catalog = self.catalog

        def describe(row):
            return catalog.reasons.get(row.get('reason')) or STATE_TEXT.get(row['status'], '尚未取得状态')

        # Items sharing a reason share one line (a failed login is one fact,
        # not eighteen).
        grouped: dict[str, list[str]] = {}
        for item in checklist.items:
            for entry in ([] if item.done else [item]) + [
                Item(c['task_id'], c) for c in item.issues
            ]:
                why = self._why(entry)
                if why:
                    titles = grouped.setdefault(why, [])
                    if catalog.title(entry.key) not in titles:
                        titles.append(catalog.title(entry.key))
        reasons = [_titles(titles) + '：' + why for why, titles in grouped.items()]
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

    def _in_force(self, snapshot) -> CardRenderer:
        """This renderer, or one on the idle times of the phases whose work is out."""
        rows = snapshot.get('tasks') if type(snapshot) is dict else None
        idle = idle_phases(self.catalog, self.schedule, rows)
        return CardRenderer(self.catalog, self.schedule.with_idle(idle)) if idle else self

    def render(self, snapshot, icons=None) -> dict:
        """The card; a phase whose work is out is shown on its idle times."""
        return self._in_force(snapshot)._render_card(snapshot, icons)

    def render_full(self, snapshot) -> dict:
        """The card with every detail, for the web board: no Lark size limits apply."""
        source = snapshot if type(snapshot) is dict else {}
        return self._in_force(snapshot)._render(source, details=True, compact=False)

    def verdicts(self, snapshot) -> dict:
        """``{task: 1 done, 0 not done, -1 not counted}`` as the card counts today.

        Metrics use it so a task the card shows done for today (a day-done
        reason, every slot run) is not reported as missed. A task with no flow
        or a missed window is not counted, and neither is one the card omits.
        """
        renderer = self._in_force(snapshot)
        checklist = renderer.checklist(snapshot if type(snapshot) is dict else {})
        return {
            item.key: -1 if renderer.off_today(item) else int(item.done) for item in checklist.items
        }

    def _render_card(self, snapshot, icons=None) -> dict:
        """The card for a snapshot; ``icons`` may name an uploaded ``spinner`` image key."""
        source = snapshot if type(snapshot) is dict else {}
        spinner = (icons or {}).get('spinner')
        running_icon = (
            {'tag': 'custom_icon', 'img_key': spinner}
            if type(spinner) is str and spinner.startswith('img_')
            else None
        )
        card = self._render(source, details=True, compact=False, running_icon=running_icon)
        if self._too_big(card):
            card = self._render(source, details=False, compact=False, running_icon=running_icon)
        if self._too_big(card):
            # Finished lines, folded away, give up their icons first.
            card = self._render(source, details=False, compact='done', running_icon=running_icon)
        if self._too_big(card):
            # Keep every item and every failure; only the layout gets denser.
            card = self._render(source, details=False, compact=True)
        if self.tags(card) > TAG_LIMIT:
            # Still too many elements: neighbouring lines share one markdown block.
            card = self._pack(card)
        return card

    def _too_big(self, card: dict) -> bool:
        return self._size(card) > CARD_LIMIT or self.tags(card) > TAG_LIMIT

    @staticmethod
    def _size(card: dict) -> int:
        return len(json.dumps(card, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))

    @staticmethod
    def tags(card) -> int:
        """The elements and components Lark counts towards its per-card limit."""
        count, stack = 0, [card]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                count += 'tag' in node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return count

    @classmethod
    def _pack(cls, node):
        """Merge each run of plain markdown lines with the same look into one block."""
        if isinstance(node, dict):
            return {key: cls._pack(value) for key, value in node.items()}
        if not isinstance(node, list):
            return node
        packed = []
        for item in map(cls._pack, node):
            last = packed[-1] if packed else None
            if (
                cls._plain(item)
                and cls._plain(last)
                and (item.get('text_size'), item.get('margin'))
                == (last.get('text_size'), last.get('margin'))
            ):
                packed[-1] = dict(last, content=last['content'] + '\n' + item['content'])
            else:
                packed.append(item)
        return packed

    @staticmethod
    def _plain(node) -> bool:
        return (
            isinstance(node, dict)
            and node.get('tag') == 'markdown'
            and set(node) <= {'tag', 'content', 'text_size', 'margin'}
        )

    def _render(self, source: dict, *, details: bool, compact, running_icon=None) -> dict:
        """``compact`` is False, 'done' (only finished lines are plain text) or True."""
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
        recurring = self.recurring()
        daily_items = [i for i in items if catalog.tasks[i.key].section not in recurring]
        repeating = [i for i in items if catalog.tasks[i.key].section in recurring]
        done = sum(item.done for item in daily_items)
        total = sum(not self.off_today(item) for item in daily_items)
        failed_phases = [
            row for row in phases.values() if row.get('status') in ('failed', 'result_unavailable')
        ]
        problems = sum(1 for item in items if self.is_problem(item.row) or item.issues)
        problems += len(failed_phases)
        streaky = sum(
            1
            for item in items
            if item.status == 'failed'
            and _number(item.row.get('fail_streak'))
            and item.row['fail_streak'] >= STREAK_ALERT
        )
        active_items = [
            item for item in items if item.status == 'running' or item.active is not None
        ]
        running_phases = [row for row in phases.values() if row.get('status') == 'running']
        running = bool(source.get('running')) or bool(running_phases) or bool(active_items)
        running_text = running_phase = None
        if running:
            phase = running_phases[-1] if running_phases else None
            running_phase = catalog.phases[phase['phase']] + '阶段' if phase else None
            names = [catalog.title((item.active or item.row)['task_id']) for item in active_items]
            parts = ([running_phase] if running_phase else []) + names[:2]
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
        shown = [p for p in self.schedule.slot_map if catalog.phase_sections.get(p, p) in
                 {section.key for section, _ in checklist.sections}]  # fmt: skip
        if running_text:
            # The round's current step always spins, even before (or without)
            # a task reporting its own start, e.g. during read-only discovery.
            elements.append(self._running_line(running_text, compact, running_icon))
        elements.append(self._plan_element(agenda, shown))
        upper = [(s, i) for s, i in checklist.sections if s.key in recurring]
        lower = [(s, i) for s, i in checklist.sections if s.key not in recurring]
        for title, group in (('循环任务', upper), ('每日任务', lower)):
            if upper and lower:
                elements.append(self._markdown(f'**{title}**'))
            for section, section_items in group:
                runs = None
                if section.key in recurring:
                    phase = recurring[section.key]
                    count, slots, strip, due, missed = self._runs(phase, source, agenda)
                    upcoming = (
                        (agenda.runs[0] if agenda.runs else agenda.first_tomorrow)
                        if due
                        else self._next_run(phase, agenda)
                    )
                    runs = (count, slots, strip, upcoming, missed)
                elements.append(
                    self._section_element(
                        section, section_items, phases, agenda, compact, running_icon, runs
                    )
                )
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
        if streaky:
            tags.append((f'{streaky} 项连续失败', 'red'))

        counts = f'每日清单 · 已完成 {done}/{total}'
        if repeating:
            counts = f'每日 {done}/{total} · 循环 {len(repeating)} 项'
        # The chat list shows this plain text and refreshes it with every update
        # (the group name already says which game), so it leads with the state.
        if running:
            summary = ' · '.join(['执行中', *filter(None, [running_phase]), f'{done}/{total}'])
        elif total and done == total and not problems:
            summary = f'全部完成 {done}/{total}'
        elif not source.get('discovered'):
            summary = f'读取中 · {done}/{total}'
        elif agenda.runs:
            when = self._when(agenda.runs[0], agenda).replace('今日 ', '')
            summary = f'下次 {when} · {done}/{total}'
        else:
            summary = f'今日已结束 · {done}/{total}'
        if problems:
            summary += f' · {problems} 项异常'
        if streaky:
            summary += f' · {streaky} 项连续失败'
        title = f'{catalog.display_name} · {day.month}月{day.day}日 周{WEEKDAYS[day.weekday()]}'
        return {
            'schema': '2.0',
            'config': {'update_multi': True, 'summary': {'content': summary}},
            'header': {
                'template': template,
                'title': {'tag': 'plain_text', 'content': title},
                'subtitle': {'tag': 'plain_text', 'content': counts},
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
