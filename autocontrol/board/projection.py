"""In-memory, allowlisted projection of one service day's checklist.

The projection aggregates worker progress events and phase results into a
display snapshot. It has no game, network or file access. A saved snapshot may
restore what a person sees, but it never decides what the worker executes.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime

from .catalog import ERROR_CODE_UNITS, STATUSES, Catalog, idle_phases
from ..schedule import SAME_TIME, Schedule

EVENT_KINDS = frozenset(
    (
        'plan',
        'task_started',
        'task_progress',
        'task_finished',
        'task_failed',
        'phase_started',
        'phase_finished',
        'phase_failed',
    )
)
RETAINED = 'completed_observation_retained'
HARVESTED = 'round_harvested'
# How a task got done today, when that is worth a note on the card.
DONE_HOW = frozenset(('retry', 'confirm', 'manual'))
TROUBLE = frozenset(('failed', 'unknown'))
SETTLED_STATES = ('incomplete', 'waiting', 'blocked')
# A closing window leaves these as they were: the outcome is unknown or impossible.
KEPT_AT_CLOSE = ('result_unavailable', 'unsupported')


def _number(value) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


def _valid_day(value) -> bool:
    try:
        return type(value) is str and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


class Projection:
    """Aggregate worker facts for display; never read by execution policy."""

    def __init__(self, catalog: Catalog, schedule: Schedule, now=None, previous=None, earlier=None):
        self.catalog = catalog
        self.schedule = schedule
        self._now = schedule.local(now)
        if self._now is None:
            raise ValueError('invalid board time')
        self.day = schedule.service_day(self._now)
        self._event_sequences: dict[str, int] = {}
        self._discovered = False
        self._running = False
        self._observed_at = None
        self._finished_at = None
        self._server_day = False
        self._round_phases: tuple[str, ...] = ()
        # When each slotted phase ran successfully today, and when a run failed
        # (display only). Kept as times: a phase on its idle times counts them
        # against fewer, longer windows (see effective_schedule).
        self._slot_runs: dict[str, set[datetime]] = {}
        self._slot_fails: dict[str, set[datetime]] = {}
        # Window tasks the server showed as part of today (open now or later).
        self._opened: set[str] = set()
        self._restoring = False
        self._manual = False
        # How many rounds in a row each task was reported failed (display and
        # metrics only). It outlives a day: a daily task that fails every day
        # is the case worth seeing, so a new day starts from the last card.
        self._fail_streaks: dict[str, int] = {}
        self._clear()
        if type(previous) is dict and previous.get('service_day') == self.day:
            # Saved windows index the slots that applied when it was saved.
            then = schedule.with_idle(idle_phases(catalog, schedule, previous.get('tasks')))
            saved = previous.get('slot_runs')
            for phase, indexes in saved.items() if type(saved) is dict else ():
                for index in indexes if type(indexes) is list else ():
                    self._mark_index(phase, index, then, self._slot_runs)
            saved = previous.get('slot_fails')
            for phase, indexes in saved.items() if type(saved) is dict else ():
                for index in indexes if type(indexes) is list else ():
                    self._mark_index(phase, index, then, self._slot_fails)
            # Restore what was displayed, plus the wake times earlier rounds
            # reported (see _carried). A claim that an earlier worker is still
            # running is never restored.
            self._carry(previous)
            rows = previous.get('tasks')
            self._restoring = True
            for source in rows if type(rows) is list else ():
                if type(source) is dict:
                    key = catalog.task_id(source.get('task_id'))
                    if key is not None:
                        if source.get('opened') is True and catalog.tasks[key].demand == 'window':
                            self._opened.add(key)
                        self._update(key, source)
                        self._restore_streak(key, source)
                        if not source.get('reason'):
                            # A saved row holds an already normalized status;
                            # never guess a specific reason from it.
                            self._tasks[key].pop('reason', None)
                        self._restore_history(self._tasks[key], source)
                        if self._tasks[key]['status'] == 'running':
                            self._tasks[key].update(
                                status='result_unavailable', reason='worker_unavailable'
                            )
            self._restoring = False
            self._observed_at = self._time(previous.get('observed_at'))
        elif type(earlier) is dict and earlier.get('service_day') != self.day:
            rows = earlier.get('tasks')
            for source in rows if type(rows) is list else ():
                if type(source) is dict and source.get('status') == 'failed':
                    key = catalog.task_id(source.get('task_id'))
                    if key is not None:
                        self._restore_streak(key, source)

    # -- internal state -------------------------------------------------

    def _clear(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._phases: dict[str, dict] = {}
        self._direct: set[str] = set()
        self._harvested: set[str] = set()
        # Goals the phase that runs them reported blocked in this round.
        self._blocked: set[str] = set()
        self._dynamic: set[float] = set()
        # Who reported a server time in a result: a wake phase, or '*' for the
        # ordinary phases. A plan claims no owner: a time it reports is owned
        # through the tasks due at it (see dynamic_owners).
        self._dynamic_by: dict[float, set[str]] = {}
        # When this round's latest phase result came in: a time an earlier
        # phase's result reported that passed before it is overdue.
        self._last_result_at: datetime | None = None
        # Future times earlier rounds of the day reported, by owner (a wake
        # phase or '*'): kept until they pass or a later successful result of
        # their owner reports again, so a failed round or one that runs only
        # some phases never drops them.
        self._carried: dict[float, set[str]] = {}
        # Each phase's latest plan times; a newer plan replaces only its own.
        self._plan_wakes: dict[str | None, set[float]] = {}
        # Rows as they were when the current phase started (keep_known).
        self._known: dict[str, dict] = {}
        self._known_direct: set[str] = set()
        # Rows as they stood before this round started them: while a task runs,
        # how its day stood decides what a closed window keeps.
        self._started_from: dict[str, dict] = {}
        for key, spec in self.catalog.tasks.items():
            if spec.seed:
                self._ensure(key)

    def _mark_index(self, phase, index, schedule, marks) -> None:
        stamps = schedule.slot_stamps(phase, self.day) if type(phase) is str else []
        if type(index) is int and 0 <= index < len(stamps):
            marks.setdefault(phase, set()).add(stamps[index])

    def mark_slot(self, phase: str, when, *, failed: bool = False) -> None:
        """A successful run of a slotted phase covers the window it started in.

        A failed run only marks its window for display; the window stays due.
        """
        stamp = self.schedule.local(when)
        if (
            stamp is None
            or self.schedule.service_day(stamp) != self.day
            or phase not in self.schedule.slot_map
        ):
            return
        (self._slot_fails if failed else self._slot_runs).setdefault(phase, set()).add(stamp)

    @property
    def effective_schedule(self) -> Schedule:
        """The schedule in force: phases whose work is out run at their idle times."""
        rows = list(self._tasks.values())
        return self.schedule.with_idle(idle_phases(self.catalog, self.schedule, rows))

    def _windows(self, marks) -> dict[str, frozenset[int]]:
        schedule = self.effective_schedule
        result = {}
        for phase, stamps in marks.items():
            found = {schedule.slot_window(phase, stamp) for stamp in stamps} - {None}
            if found:
                result[phase] = frozenset(found)
        return result

    @property
    def slot_runs(self) -> dict[str, frozenset[int]]:
        return self._windows(self._slot_runs)

    def set_manual(self, manual: bool) -> None:
        """This round was started by hand (``once``), not by the schedule."""
        self._manual = bool(manual)

    def set_round(self, phases) -> None:
        """The phases this round will run, in order (display only)."""
        self._round_phases = tuple(phase for phase in phases if phase in self.catalog.phases)

    def _carry(self, previous: dict) -> None:
        saved, owners = previous.get('dynamic_candidates'), previous.get('dynamic_owners')
        owners = owners if type(owners) is dict else {}
        seen = previous.get('observed_at')
        seen = self.schedule.local(seen) if type(seen) is str else None
        floor = max(self._now, seen) if seen is not None else self._now
        for value in saved if type(saved) is list else ():
            stamp = self.schedule.local(value) if type(value) is str else None
            if stamp is None or stamp <= floor:
                continue  # already passed when it was saved
            found = owners.get(value)
            names = {name for name in found if type(name) is str} if type(found) is list else set()
            self._carried[stamp.timestamp()] = names or {'*'}

    def _restore_streak(self, key: str, source: dict) -> None:
        value = source.get('fail_streak')
        if type(value) is int and 0 < value <= 100_000 and self.catalog.tasks[key].kind != 'step':
            self._fail_streaks[key] = value

    def _wakes(self) -> set[float]:
        now = self._now.timestamp()
        return {stamp for stamp in self._dynamic | set(self._carried) if stamp > now}

    @property
    def dynamic_candidates(self) -> tuple[float, ...]:
        """Future times the worker reported today: this round's, and earlier ones still due."""
        return tuple(sorted(self._wakes()))

    @property
    def dynamic_owners(self) -> dict[float, frozenset[str]]:
        """Who each server time belongs to, when the schedule has wake phases.

        A wake phase owns a time its own result reported, or one a task of its
        section is due at; anything else belongs to the ordinary phases ('*').
        Times without any evidence are left out (the ordinary phases run).
        """
        return self._owners_for(self._wakes())

    def overdue_wakes(self) -> dict[float, frozenset[str]]:
        """Times this round's phase results reported that passed during the round.

        A time counts when it passed before the round's latest phase result
        came in: a later phase still ran when it was due. Each maps to its
        owners (as in dynamic_owners; empty when unknown). The board keeps
        only future times; the service runs these right after the round.
        """
        if self._last_result_at is None:
            return {}
        limit = self._last_result_at.timestamp()
        stamps = sorted(stamp for stamp in self._dynamic_by if stamp <= limit)
        owners = self._owners_for(stamps)
        return {stamp: owners.get(stamp, frozenset()) for stamp in stamps}

    def _owners_for(self, stamps) -> dict[float, frozenset[str]]:
        wake = set(self.schedule.wake_phases)
        if not wake:
            return {}
        sections = {section: phase for phase, section in self.catalog.phase_sections.items()}
        due = []
        for key, row in self._tasks.items():
            stamp = self.schedule.local(row['next_run_at']) if row.get('next_run_at') else None
            if stamp is not None:
                spec = self.catalog.tasks[key]
                phase = spec.phase or sections.get(spec.section)
                due.append((stamp.timestamp(), phase if phase in wake else '*'))
        window = SAME_TIME.total_seconds()
        owners = {}
        for stamp in stamps:
            found = set(self._dynamic_by.get(stamp, ())) | self._carried.get(stamp, set())
            found |= {owner for when, owner in due if abs(when - stamp) < window}
            if found:
                owners[stamp] = frozenset(found)
        return owners

    def _time(self, value) -> str | None:
        stamp = self.schedule.local(value) if value is not None else None
        return stamp.isoformat(timespec='seconds') if stamp is not None else None

    def _observe(self, value) -> None:
        stamp = self.schedule.local(value) if value is not None else None
        if stamp is not None:
            self._now = max(self._now, stamp)
            self._observed_at = stamp.isoformat(timespec='seconds')

    def _counts(self, value) -> dict:
        if type(value) is not dict:
            return {}
        return {
            key: value[key] for key in sorted(self.catalog.count_keys) if _number(value.get(key))
        }

    def _ensure(self, key: str) -> dict:
        if key not in self._tasks:
            spec = self.catalog.tasks[key]
            row = {
                'task_id': key,
                'title': spec.title,
                'phase': spec.section,
                'level': spec.kind,
                'status': 'not_started',
                'availability': 'unknown',
                'counts': {},
            }
            if spec.kind == 'step':
                row['parent_task'] = spec.parent
            self._tasks[key] = row
        return self._tasks[key]

    def _update(self, key, source, *, baseline: bool = False, final: bool = False) -> None:
        catalog = self.catalog
        key = catalog.task_id(key)
        if key is None or type(source) is not dict:
            return
        spec = catalog.tasks[key]
        row = self._ensure(key)
        if baseline and spec.kind != 'goal' and key in self._direct:
            return
        counts_source = source.get('counts', source)
        # A window activity the server showed today and has now closed keeps
        # the day's outcome: done (or done for today) stays done, an unknown or
        # unsupported row stays as it was, otherwise today's chance passed. A
        # task that started this round and then found the window closed is
        # judged by how it stood before it started; a start alone proves nothing.
        if (
            spec.demand == 'window'
            and key in self._opened
            and (
                source.get('availability') == 'closed'
                or type(source.get('status')) is str
                and catalog.normalize(source['status'], self._counts(counts_source)) == 'closed'
            )
        ):
            before = self._started_from.pop(key, None) if row['status'] == 'running' else None
            if before is not None:
                row.clear()
                row.update(before)
            if not (self._finished(row) or row['status'] in (*KEPT_AT_CLOSE, 'running')):
                row.update(status='incomplete', reason='window_ended', availability='open')
                row.pop('next_run_at', None)
            return
        # A per-round task this round's own job already harvested stays done
        # for the round; a later re-read of its regenerated state (pet decay,
        # accrued income) does not reopen it. The next round decides afresh.
        if (
            baseline
            and key in self._harvested
            and catalog.normalize(source.get('status'), self._counts(counts_source)) != 'complete'
        ):
            return
        # A failure observed in this round stays visible until the server shows
        # the goal complete; a later aggregate such as "incomplete" must not
        # silently replace the concrete reason.
        if (
            baseline
            and key in self._direct
            and (row['status'] == 'failed' or row.get('reason') in catalog.request_problems)
            and catalog.normalize(source.get('status'), self._counts(counts_source)) != 'complete'
        ):
            return
        # Likewise a concrete block observed in this round (e.g. 体力不足) is
        # more specific than a later bare "incomplete" or "not started".
        if (
            baseline
            and (key in self._direct or key in self._blocked)
            and row['status'] == 'blocked'
            and catalog.normalize(source.get('status'), self._counts(counts_source))
            in ('incomplete', 'not_started')
        ):
            return
        # A later session lacking a provider snapshot is absence of evidence,
        # not evidence that a state observed earlier today was undone. A bare
        # "unknown" (no request-problem reason) never replaces a concrete state;
        # it may only carry a fresh due time.
        bare_unknown = (
            type(source.get('status')) is str
            and catalog.normalize(source.get('status'), self._counts(counts_source))
            == 'result_unavailable'
            and catalog.reason(source, 'result_unavailable') not in catalog.request_problems
        )
        # Work done earlier today that this round started again is judged by
        # how it stood before it started: the start alone proves nothing, so
        # such an unknown keeps the day's work done. Per-round work (收菜) is
        # left to this round's own outcome.
        if bare_unknown and row['status'] == 'running':
            before = self._started_from.get(key)
            if (
                before is not None
                and self._finished(before)
                and key not in catalog.round_tasks
                and spec.demand != 'round'
            ):
                del self._started_from[key]
                row.clear()
                row.update(before)
        if bare_unknown and row['status'] in (
            'complete',
            'waiting',
            'blocked',
            'incomplete',
            'failed',
            'unsupported',
        ):
            if row['status'] == 'complete':
                row['reason'] = RETAINED
            when = self._time(source.get('next_run_at'))
            if spec.next_run_count and self._counts(counts_source).get(spec.next_run_count, 0) > 0:
                when = self._time(self._counts(counts_source)[spec.next_run_count])
            if when is not None and row['status'] != 'complete':
                row['next_run_at'] = when
            return
        # A phase that deliberately defers a target must not erase a fact
        # another phase already observed for the same day.
        if baseline and source.get('status') == 'not_started' and row['status'] != 'not_started':
            return
        # A window activity done today stays done: a later read-only plan that
        # cannot see today's participation does not undo it. (A new window of
        # the same activity later today is reported as waiting, not_open.)
        if (
            baseline
            and spec.demand == 'window'
            and self._standing(key, row)['status'] == 'complete'
            and catalog.normalize(source.get('status'), self._counts(counts_source))
            in ('incomplete', 'not_started')
        ):
            return
        source_counts = self._counts(counts_source)
        if (
            type(source.get('status')) is str
            and row['status'] != 'running'
            and not self._restoring
            and catalog.normalize(source['status'], source_counts or row['counts']) == 'running'
        ):
            self._started_from[key] = deepcopy(row)
        if source_counts:
            row['counts'] = source_counts
        before = (row['status'], row.get('reason'))
        if type(source.get('status')) is str:
            was_done = self._finished(row)
            state = catalog.normalize(source['status'], row['counts'])
            if row['counts'].get('unsupported', 0) > 0:
                state = 'unsupported'
            if state != 'running':
                self._started_from.pop(key, None)
            row['status'] = state
            reason = catalog.reason(source, state)
            if reason:
                row['reason'] = reason
            else:
                row.pop('reason', None)
            self._track(row, was_done)
        if source.get('availability') in ('open', 'closed', 'unknown'):
            row['availability'] = source['availability']
        if row['status'] == 'closed':
            row['availability'] = 'closed'
        elif (
            spec.demand == 'window'
            and not self._restoring
            and row['availability'] != 'closed'
            # Only a known state shows the window is part of today; an unknown
            # status or availability is no evidence either way, and neither is a
            # task that has only started (it may find the activity closed).
            and (
                source.get('availability') == 'open'
                or row['status'] not in ('result_unavailable', 'running')
                and source.get('availability') != 'unknown'
            )
        ):
            self._opened.add(key)
        # An error code is an outcome, not persistent task progress. A new
        # direct final result must not inherit a code (or attach an old
        # progress number to a newly supplied error-code unit).
        old_unit, new_unit = row.get('unit'), source.get('unit')
        if final and (isinstance(old_unit, str) and old_unit in ERROR_CODE_UNITS
                      or isinstance(new_unit, str) and new_unit in ERROR_CODE_UNITS):
            for field in ('current', 'total', 'unit'):
                row.pop(field, None)
        for field in ('current', 'total'):
            if _number(source.get(field)):
                row[field] = source[field]
        if type(source.get('unit')) is str and source['unit'] in catalog.units:
            row['unit'] = source['unit']
        if row.get('total') == 0:
            row.pop('total', None)
        when = self._time(source.get('next_run_at'))
        if spec.next_run_count and source_counts.get(spec.next_run_count, 0) > 0:
            # A later login can observe a new batch; its authoritative next
            # completion replaces the earlier display time.
            when = self._time(source_counts[spec.next_run_count])
        if when is not None:
            row['next_run_at'] = when
        elif (
            type(source.get('status')) is str
            and row['status'] != 'running'
            and (
                row['status'] != before[0]
                # A plan's bare read may lack the reason; only the task's own
                # report changes the state by its reason alone.
                or (not baseline and row.get('reason') != before[1])
            )
        ):
            # A new state brings its own time or none: the old time belonged to
            # the old state (a failure must not keep the wait it replaced).
            row.pop('next_run_at', None)
        if spec.progress and 'current' not in source:
            for current, total in spec.progress:
                if current in row['counts'] and row['counts'].get(total, 0) > 0:
                    row.update(current=row['counts'][current], total=row['counts'][total])
                    if spec.unit:
                        row['unit'] = spec.unit
                    break

    def _harvest(self, key, source) -> None:
        """Mark a per-round task harvested by this round's own direct observation."""
        extra = self.catalog.round_tasks.get(key)
        row = self._tasks.get(key)
        if extra is None or row is None or type(source) is not dict:
            return
        spec = self.catalog.tasks[key]
        target = spec.parent if spec.kind == 'step' else key
        raw = [source.get(field) for field in ('status', 'reason')]
        if row['status'] == 'complete' or (
            row['status'] in ('incomplete', 'waiting', 'blocked')
            and any(type(value) is str and value in extra for value in raw)
        ):
            # A step reporting under its parent's job harvests the parent too.
            for done in [self._ensure(target)] + ([row] if target != key else []):
                was_done = self._finished(done)
                done.update(status='complete', reason=HARVESTED)
                self._track(done, was_done)
            self._harvested.add(target)
        else:
            self._harvested.discard(target)

    def _merge_goals(self, summary, *, baseline: bool = False, owner: str | None = None) -> None:
        if type(summary) is not dict or type(summary.get('goals')) is not dict:
            return
        for key, source in summary['goals'].items():
            spec = self.catalog.tasks.get(key)
            if spec is not None and spec.kind in self.catalog.plan_kinds and type(source) is dict:
                # The phase that runs this goal sees it after its own jobs and
                # may lift its block; later sessions may lack that evidence
                # (e.g. a loaded wardrobe), so only it pins a block.
                owned = owner is not None and self.catalog.run_section(key) == owner
                if owned:
                    self._blocked.discard(key)
                self._update(key, source, baseline=baseline)
                if owned and self._tasks[key]['status'] == 'blocked':
                    self._blocked.add(key)

    def _set_phase(self, phase: str, state: str, reason: str | None) -> None:
        self._phases[phase] = {'phase': phase, 'title': self.catalog.phases[phase], 'status': state}
        if reason:
            self._phases[phase]['reason'] = reason
        self._running = any(row['status'] == 'running' for row in self._phases.values())

    # -- inputs ------------------------------------------------------------

    def apply_event(self, event) -> None:
        """Apply one streamed worker event (``plan``, ``task_*`` or ``phase_*``)."""
        if type(event) is not dict:
            return
        catalog = self.catalog
        kind = event.get('kind')
        if kind not in EVENT_KINDS:
            return
        phase = event.get('phase')
        if type(phase) is not str or phase not in catalog.phases:
            phase = None
        plan = event.get('plan') if kind == 'plan' else None
        event_day = event.get('game_day')
        if type(plan) is dict:
            event_day = plan.get('game_day', plan.get('service_day', event_day))
        if type(event_day) is str and _valid_day(event_day):
            if not self._server_day and kind == 'plan':
                # The first live server day supersedes the computer's guess.
                if event_day != self.day:
                    self.day = event_day
                    self._slot_runs = {}  # slot windows and openings belong to a day
                    self._slot_fails = {}
                    self._opened = set()
                    self._clear()
                self._server_day = True
            elif event_day != self.day:
                return
        generation, sequence = event.get('generation'), event.get('sequence', event.get('seq'))
        if type(generation) is str and len(generation) <= 64 and type(sequence) is int:
            if sequence <= self._event_sequences.get(generation, -1):
                return
            self._event_sequences[generation] = sequence
        self._observe(event.get('observed_at'))
        if kind == 'plan':
            if type(plan) is not dict:
                return
            self._discovered = True
            self._merge_goals(plan.get('full_checklist_summary', plan), baseline=True)
            candidates = plan.get('dynamic_candidates')
            if type(candidates) in (list, tuple):
                # A phase's newer plan replaces only what its own earlier plan
                # reported; times other phases reported this round stay.
                self._plan_wakes[phase] = {
                    stamp.timestamp()
                    for value in candidates
                    if value is not None
                    and (stamp := self.schedule.local(value)) is not None
                    and stamp > self._now
                }
                self._dynamic = set(self._dynamic_by).union(*self._plan_wakes.values())
            return
        if kind.startswith('phase_'):
            if kind == 'phase_started':
                self._checkpoint()
            if phase is None:
                return
            if kind == 'phase_started':
                state = 'running'
            elif kind == 'phase_failed':
                state = 'failed'
            else:
                state = catalog.normalize(event.get('status'))
            self._set_phase(phase, state, catalog.reason(event, state))
            return
        key = catalog.task_id(event.get('task_id'))
        if key is None:
            return
        source = dict(event)
        if kind == 'task_started':
            source['status'] = 'running'
            self._running = True
        elif kind == 'task_failed':
            if event.get('status') not in ('uncertain', 'timeout'):
                source['status'] = 'failed'
        elif kind == 'task_progress' and 'status' not in source:
            source['status'] = 'running'
        self._update(key, source, final=kind in ('task_finished', 'task_failed'))
        self._harvest(key, source)
        if kind != 'task_started':
            self._direct.add(key)

    def apply_result(self, result) -> None:
        """Apply a finished phase's facts, as translated by the project adapter.

        ``result`` holds ``phase``, optional ``game_day``, baseline ``goals``,
        ordered direct ``updates`` (``[(task, source), ...]``), the raw
        ``execution`` state, a fixed failure ``reason``, ``finished_at`` and
        optional future ``dynamic_candidates`` reported by the worker.
        """
        if type(result) is not dict:
            return
        catalog = self.catalog
        phase = result.get('phase')
        if type(phase) is not str or phase not in catalog.phases:
            return
        day = result.get('game_day')
        if day is not None and day != self.day:
            return
        self._observe(result.get('finished_at'))
        goals = result.get('goals')
        self._merge_goals(
            {'goals': goals} if type(goals) is dict else None,
            baseline=True,
            owner=catalog.phase_sections.get(phase),
        )
        for item in result.get('updates') or ():
            if type(item) in (list, tuple) and len(item) == 2:
                key = catalog.task_id(item[0])
                source = item[1]
                final = type(source) is dict and type(source.get('status')) is str and source['status'] != 'running'
                self._update(key, source, final=final)
                if key is not None:
                    self._harvest(key, item[1])
                    self._direct.add(key)
        state = catalog.normalize(result.get('execution'))
        reason = None
        if state in ('failed', 'result_unavailable'):
            reason = result.get('reason')
            if type(reason) is not str or reason not in catalog.reasons:
                reason = catalog.reason(result, state)
        self._set_phase(phase, state, reason)
        self._observe(result.get('finished_at'))
        owner = phase if phase in self.schedule.wake_phases else '*'
        if state in ('complete', 'incomplete', 'waiting'):
            # A phase that ran reports all of its own times again.
            for stamp in list(self._carried):
                self._carried[stamp].discard(owner)
                if not self._carried[stamp]:
                    del self._carried[stamp]
        candidates = result.get('dynamic_candidates')
        if type(candidates) in (list, tuple):
            for value in candidates:
                stamp = self.schedule.local(value) if value is not None else None
                if stamp is not None and stamp > self._now:
                    self._dynamic.add(stamp.timestamp())
                    self._dynamic_by.setdefault(stamp.timestamp(), set()).add(owner)
        self._last_result_at = self._now

    def _standing(self, key, row) -> dict:
        """How the day stands for a row: while it runs, as it was before it started."""
        if row['status'] == 'running':
            return self._started_from.get(key, row)
        return row

    def _finished(self, row) -> bool:
        """Done today: complete, or nothing more can happen before the next day."""
        return row['status'] == 'complete' or (
            row['status'] in SETTLED_STATES and row.get('reason') in self.catalog.day_done_reasons
        )

    def _track(self, row, was_done: bool) -> None:
        """Remember when and how a task got done today (display only)."""
        if self._restoring:
            return
        if row['status'] == 'failed':
            row['trouble'] = 'failed'
        elif (
            row['status'] == 'result_unavailable'
            and row.get('reason') in self.catalog.request_problems
            and row.get('trouble') != 'failed'
        ):
            row['trouble'] = 'unknown'
        done = self._finished(row)
        key = row['task_id']
        per_round = key in self.catalog.round_tasks or self.catalog.tasks[key].demand == 'round'
        if done and not was_done:
            trouble = row.pop('trouble', None)
            if per_round or 'done_at' not in row:
                # The card shows when the day's work was first done; reopening
                # and finishing again (a later window, optional extras) keeps
                # that time. Per-round work shows this round's harvest.
                row['done_at'] = self._now.isoformat(timespec='seconds')
                how = {'failed': 'retry', 'unknown': 'confirm'}.get(trouble)
                if how is None and self._manual:
                    how = 'manual'
                if how:
                    row['done_how'] = how
                else:
                    row.pop('done_how', None)
        elif was_done and not done and per_round:
            row.pop('done_at', None)
            row.pop('done_how', None)

    def _restore_history(self, row, source) -> None:
        stamp = self.schedule.local(source.get('done_at')) if source.get('done_at') else None
        if stamp is not None and type(source.get('done_at')) is str:
            row['done_at'] = stamp.isoformat(timespec='seconds')
        if source.get('done_how') in DONE_HOW:
            row['done_how'] = source['done_how']
        if source.get('trouble') in TROUBLE:
            row['trouble'] = source['trouble']

    def _checkpoint(self) -> None:
        self._known = deepcopy(self._tasks)
        self._known_direct = set(self._direct)

    def keep_known(self) -> None:
        """Undo the unknowns of a phase that provably sent no request.

        Such a phase cannot have changed anything on the server, so an unknown
        it produced (a failed login reported for every task, a session lost
        before any request) is absence of evidence: each row it left unknown,
        running or marked with a request problem is shown as it was when the
        phase started, and the failure shows on the phase row only. New
        concrete facts the phase did observe are kept.
        """
        problems = self.catalog.request_problems
        for key, before in self._known.items():
            row = self._tasks.get(key)
            if row is None or row == before:
                continue
            if row['status'] in ('result_unavailable', 'running') or row.get('reason') in problems:
                self._tasks[key] = deepcopy(before)
                if before['status'] != 'running':
                    self._started_from.pop(key, None)
                if key in self._known_direct:
                    self._direct.add(key)
                else:
                    self._direct.discard(key)

    def finish_run(self, now=None) -> None:
        """Close the round: anything still running ended without a result."""
        for row in (*self._tasks.values(), *self._phases.values()):
            if row['status'] == 'running':
                row.update(status='result_unavailable', reason='ended_without_result')
                if 'task_id' in row:
                    self._track(row, False)
        self._started_from.clear()
        for key in self._direct:
            row = self._tasks.get(key)
            if row is None or self.catalog.tasks[key].kind == 'step':
                continue
            if row['status'] == 'failed':
                self._fail_streaks[key] = self._fail_streaks.get(key, 0) + 1
            else:
                self._fail_streaks.pop(key, None)
        self._running = False
        self._observe(self.schedule.now() if now is None else now)
        self._finished_at = self._observed_at

    # -- output --------------------------------------------------------------

    def snapshot(self) -> dict:
        catalog, schedule = self.catalog, self.schedule
        # 'seen': a worker reported this task during the current round.
        tasks = [
            {
                **row,
                'seen': key in self._direct,
                **({'opened': True} if key in self._opened else {}),
                **({'fail_streak': self._fail_streaks[key]} if key in self._fail_streaks else {}),
            }
            for key, row in self._tasks.items()
        ]
        sections_with_jobs = {
            catalog.tasks[key].section for key in self._tasks if catalog.tasks[key].kind == 'job'
        }
        replaced = {
            section.aggregate
            for section in catalog.sections
            if section.aggregate and section.key in sections_with_jobs
        }
        counted = [
            row
            for row in tasks
            if row['level'] != 'step'
            and row['task_id'] not in replaced
            and row['availability'] != 'closed'
        ]
        summary = {state: sum(row['status'] == state for row in counted) for state in STATUSES}
        summary.update(
            completed=summary['complete'],
            unavailable=summary['result_unavailable'],
            total=len(counted),
        )
        result = {
            'schema': 1,
            'project': catalog.project,
            'service_day': self.day,
            'timezone': schedule.tz_name,
            'reset_at': schedule.reset,
            'discovered': self._discovered,
            'running': self._running,
            'tasks': tasks,
            'phases': list(self._phases.values()),
            'summary': summary,
            'next_check_at': self.effective_schedule.next_check(
                self._now, self.dynamic_candidates
            ).isoformat(timespec='seconds'),
            'dynamic_candidates': [
                datetime.fromtimestamp(value, schedule.tz).isoformat(timespec='seconds')
                for value in self.dynamic_candidates
            ],
            'dynamic_source': 'server_observation' if self._dynamic else None,
        }
        owners = self.dynamic_owners
        if owners:
            result['dynamic_owners'] = {
                datetime.fromtimestamp(stamp, schedule.tz).isoformat(timespec='seconds'): sorted(
                    found
                )
                for stamp, found in sorted(owners.items())
            }
        if self._round_phases:
            result['round_phases'] = list(self._round_phases)
        for key, marks in (('slot_runs', self._slot_runs), ('slot_fails', self._slot_fails)):
            windows = self._windows(marks)
            if windows:
                result[key] = {phase: sorted(found) for phase, found in sorted(windows.items())}
        if self._observed_at is not None:
            result['observed_at'] = self._observed_at
        if self._finished_at is not None:
            result['finished_at'] = self._finished_at
        return deepcopy(result)
