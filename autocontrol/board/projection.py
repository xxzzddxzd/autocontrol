"""In-memory, allowlisted projection of one service day's checklist.

The projection aggregates worker progress events and phase results into a
display snapshot. It has no game, network or file access. A saved snapshot may
restore what a person sees, but it never decides what the worker executes.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime

from .catalog import STATUSES, Catalog
from ..schedule import Schedule

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


def _number(value) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


def _valid_day(value) -> bool:
    try:
        return type(value) is str and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


class Projection:
    """Aggregate worker facts for display; never read by execution policy."""

    def __init__(self, catalog: Catalog, schedule: Schedule, now=None, previous=None):
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
        # Slot windows (per slotted phase) that already had a successful run.
        self._slot_runs: dict[str, set[int]] = {}
        # Window tasks the server showed as part of today (open now or later).
        self._opened: set[str] = set()
        self._restoring = False
        self._clear()
        if type(previous) is dict and previous.get('service_day') == self.day:
            saved = previous.get('slot_runs')
            for phase, indexes in saved.items() if type(saved) is dict else ():
                for index in indexes if type(indexes) is list else ():
                    self._mark_index(phase, index)
            # Restore what was displayed only. Wake candidates and a claim that
            # an earlier worker is still running are never restored.
            rows = previous.get('tasks')
            self._restoring = True
            for source in rows if type(rows) is list else ():
                if type(source) is dict:
                    key = catalog.task_id(source.get('task_id'))
                    if key is not None:
                        if source.get('opened') is True and catalog.tasks[key].demand == 'window':
                            self._opened.add(key)
                        self._update(key, source)
                        if not source.get('reason'):
                            # A saved row holds an already normalized status;
                            # never guess a specific reason from it.
                            self._tasks[key].pop('reason', None)
                        if self._tasks[key]['status'] == 'running':
                            self._tasks[key].update(
                                status='result_unavailable', reason='worker_unavailable'
                            )
            self._restoring = False
            self._observed_at = self._time(previous.get('observed_at'))

    # -- internal state -------------------------------------------------

    def _clear(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._phases: dict[str, dict] = {}
        self._direct: set[str] = set()
        self._harvested: set[str] = set()
        # Goals the phase that runs them reported blocked in this round.
        self._blocked: set[str] = set()
        self._dynamic: set[float] = set()
        for key, spec in self.catalog.tasks.items():
            if spec.seed:
                self._ensure(key)

    def _mark_index(self, phase, index) -> None:
        stamps = self.schedule.slot_stamps(phase, self.day) if type(phase) is str else []
        if type(index) is int and 0 <= index < len(stamps):
            self._slot_runs.setdefault(phase, set()).add(index)

    def mark_slot(self, phase: str, when) -> None:
        """A successful run of a slotted phase covers the window it started in."""
        stamp = self.schedule.local(when)
        if stamp is None or self.schedule.service_day(stamp) != self.day:
            return
        index = self.schedule.slot_window(phase, stamp)
        if index is not None:
            self._mark_index(phase, index)

    @property
    def slot_runs(self) -> dict[str, frozenset[int]]:
        return {phase: frozenset(indexes) for phase, indexes in self._slot_runs.items()}

    def set_round(self, phases) -> None:
        """The phases this round will run, in order (display only)."""
        self._round_phases = tuple(phase for phase in phases if phase in self.catalog.phases)

    @property
    def dynamic_candidates(self) -> tuple[float, ...]:
        """Future times observed by this process's own worker, never saved data."""
        return tuple(sorted(self._dynamic))

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

    def _update(self, key, source, *, baseline: bool = False) -> None:
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
        # the day's outcome: done stays done, otherwise today's chance passed.
        if (
            spec.demand == 'window'
            and key in self._opened
            and (
                source.get('availability') == 'closed'
                or type(source.get('status')) is str
                and catalog.normalize(source['status'], self._counts(counts_source)) == 'closed'
            )
        ):
            if row['status'] != 'complete':
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
        if (
            type(source.get('status')) is str
            and catalog.normalize(source.get('status'), self._counts(counts_source))
            == 'result_unavailable'
            and catalog.reason(source, 'result_unavailable') not in catalog.request_problems
            and row['status']
            in ('complete', 'waiting', 'blocked', 'incomplete', 'failed', 'unsupported')
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
        source_counts = self._counts(counts_source)
        if source_counts:
            row['counts'] = source_counts
        if type(source.get('status')) is str:
            state = catalog.normalize(source['status'], row['counts'])
            if row['counts'].get('unsupported', 0) > 0:
                state = 'unsupported'
            row['status'] = state
            reason = catalog.reason(source, state)
            if reason:
                row['reason'] = reason
            else:
                row.pop('reason', None)
        if source.get('availability') in ('open', 'closed', 'unknown'):
            row['availability'] = source['availability']
        if row['status'] == 'closed':
            row['availability'] = 'closed'
        elif spec.demand == 'window' and not self._restoring and row['availability'] != 'closed':
            self._opened.add(key)
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
            self._ensure(target).update(status='complete', reason=HARVESTED)
            if target != key:
                row.update(status='complete', reason=HARVESTED)
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
                owned = owner is not None and spec.section == owner
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
                self._dynamic = {
                    stamp.timestamp()
                    for value in candidates
                    if value is not None
                    and (stamp := self.schedule.local(value)) is not None
                    and stamp > self._now
                }
            return
        if kind.startswith('phase_'):
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
        self._update(key, source)
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
        goals = result.get('goals')
        self._merge_goals(
            {'goals': goals} if type(goals) is dict else None,
            baseline=True,
            owner=catalog.phase_sections.get(phase),
        )
        for item in result.get('updates') or ():
            if type(item) in (list, tuple) and len(item) == 2:
                key = catalog.task_id(item[0])
                self._update(key, item[1])
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
        candidates = result.get('dynamic_candidates')
        if type(candidates) in (list, tuple):
            self._dynamic |= {
                stamp.timestamp()
                for value in candidates
                if value is not None
                and (stamp := self.schedule.local(value)) is not None
                and stamp > self._now
            }

    def finish_run(self, now=None) -> None:
        """Close the round: anything still running ended without a result."""
        for row in (*self._tasks.values(), *self._phases.values()):
            if row['status'] == 'running':
                row.update(status='result_unavailable', reason='ended_without_result')
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
            'next_check_at': schedule.next_check(self._now, self.dynamic_candidates).isoformat(
                timespec='seconds'
            ),
            'dynamic_candidates': [
                datetime.fromtimestamp(value, schedule.tz).isoformat(timespec='seconds')
                for value in self.dynamic_candidates
            ],
            'dynamic_source': 'server_observation' if self._dynamic else None,
        }
        if self._round_phases:
            result['round_phases'] = list(self._round_phases)
        if self._slot_runs:
            result['slot_runs'] = {
                phase: sorted(indexes) for phase, indexes in sorted(self._slot_runs.items())
            }
        if self._observed_at is not None:
            result['observed_at'] = self._observed_at
        if self._finished_at is not None:
            result['finished_at'] = self._finished_at
        return deepcopy(result)
