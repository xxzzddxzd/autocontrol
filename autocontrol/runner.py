"""One round for one project: publish the plan, run the phases, close the board.

Display state never authorizes or skips a game action. The fixed plan is
published (with a bounded wait) before the worker starts, and a notification
or metrics outage can never prevent authorized execution.
"""

from __future__ import annotations

from datetime import date, datetime
import os
import subprocess
import time
import uuid

from . import locks, protocol
from .schedule import SAME_TIME


def _valid_day(value) -> bool:
    try:
        return type(value) is str and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _code(value):
    """A fixed lower-case code from a record, or None (no free text in metrics)."""
    return (
        value
        if type(value) is str
        and len(value) <= 48
        and value.replace('_', '').isalpha()
        and value.islower()
        else None
    )


def _ms(start: str | None, end: str | None):
    try:
        return int(
            (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000
        )
    except (TypeError, ValueError):
        return None


class RoundRunner:
    def __init__(
        self,
        project,
        publisher,
        *,
        now=None,
        delivery_timeout=30.0,
        observe_timeout=None,
        metrics=None,
    ):
        self.project = project
        self.publisher = publisher
        self.metrics = metrics
        self.delivery_timeout = delivery_timeout
        self.observe_timeout = observe_timeout or project.observe_timeout
        self.projection = project.new_projection(now)
        self._views = {self.projection.day: self.projection}
        self.dynamic_candidates: tuple[float, ...] = ()
        self.dynamic_owners: dict[float, frozenset[str]] = {}
        # Times the last round's phases reported that passed during it (take_overdue).
        self.overdue: tuple[float, ...] = ()
        self._events = 0
        self.round_id = None

    # -- board plumbing ------------------------------------------------------

    def _publish(self, view=None) -> None:
        try:
            snapshot = (view or self.projection).snapshot()
            phases = snapshot.get('phases', ())
            if not snapshot.get('discovered') and not any(
                row.get('status') in ('failed', 'result_unavailable') for row in phases
            ):
                return
            snapshot['display_revision'] = time.time_ns()
            self.publisher.submit(snapshot)
        except Exception:
            pass

    def _flush(self) -> dict:
        try:
            self.publisher.flush(timeout=self.delivery_timeout)
            return dict(self.publisher.status)
        except Exception:
            return {'status': 'failed', 'error_code': 'publisher_error'}

    def _view(self, day=None):
        """The projection for a service day; a task that spans the reset keeps its day."""
        if not _valid_day(day):
            return self.projection
        if day not in self._views:
            self._views[day] = self.project.new_projection(self.project.schedule.day_anchor(day))
            self._views[day].set_round(getattr(self, '_round', ()))
            self._views[day].set_manual(getattr(self, '_manual', False))
        view = self._views[day]
        if day >= self.projection.day:
            self.projection = view
        return view

    def _event(self, event) -> None:
        self._events += 1
        plan = event.get('plan') if event.get('kind') == 'plan' else None
        day = plan.get('game_day') if type(plan) is dict else event.get('game_day')
        view = self._view(day)
        view.apply_event(event)
        self._publish(view)

    # -- metrics -------------------------------------------------------------------

    def _record(self, method, *args) -> None:
        if self.metrics is None:
            return
        try:
            getattr(self.metrics, method)(*args)
        except Exception:
            pass  # metrics never affect a round

    def _phase_row(self, phase, record, mode, events, view) -> dict:
        project = self.project
        section = project.catalog.phase_sections.get(phase)
        rows = [
            row
            for row in view.snapshot()['tasks']
            if row['level'] != 'step'
            and row['availability'] != 'closed'
            and (section is None or project.catalog.run_section(row['task_id']) == section)
        ]
        count = lambda *states: sum(row['status'] in states for row in rows)  # noqa: E731
        stage, error = _code(record.get('failure_stage')), record.get('error_type')
        row = {
            'project': project.name,
            'round_id': self.round_id,
            'service_day': view.day,
            'phase': phase,
            'mode': mode,
            'started_at': record.get('started_at'),
            'finished_at': record.get('finished_at'),
            'duration_ms': record.get(
                'duration_ms', _ms(record.get('started_at'), record.get('finished_at'))
            ),
            'exit_code': record.get('exit_code'),
            'execution': _code(record.get('execution')),
            'failure_stage': stage
            if stage in protocol.FAILURE_STAGES or stage in project.catalog.reasons
            else None,
            'error_type': error
            if type(error) is str and protocol.ERROR_TYPE.fullmatch(error)
            else None,
            'timed_out': record.get('execution') == 'timeout',
            'busy': project.is_busy(record),
            'tasks_total': len(rows),
            'tasks_complete': count('complete'),
            'tasks_failed': count('failed'),
            'tasks_unavailable': count('result_unavailable'),
            'tasks_blocked': count('blocked', 'unsupported'),
            'tasks_waiting': count('waiting'),
            'events': events,
        }
        try:
            row.update(project.phase_metrics(record))
            row.update(project.protocol_metrics(record))
        except Exception:
            pass
        return row

    # -- rounds ------------------------------------------------------------------

    def phases_for(self, now=None, *, scheduled_at=None) -> tuple[str, ...]:
        """The phases a wake runs, in round order.

        A fixed wake (or a start-up catch-up, when ``scheduled_at`` is
        ``None``) runs every phase without slots; a server-reported time runs
        the schedule's ``dynamic_phases`` (every phase without slots when
        unset). A slotted phase runs at its own slots, and at any wake while
        the window of its latest slot still lacks a successful run.
        """
        project = self.project
        now = project.schedule.local(now) or project.schedule.now()
        view = self._slot_view(now)
        schedule = view.effective_schedule  # a phase whose work is out keeps its idle times
        slots = schedule.slot_map
        fixed = scheduled_at is None or schedule.is_fixed(scheduled_at)
        dynamic = scheduled_at is not None and schedule.is_dynamic(
            scheduled_at, self.dynamic_candidates
        )
        done = view.slot_runs
        owners = self._owners(scheduled_at) if dynamic else None
        wake = set(schedule.wake_phases)
        # A server time that only wake phases reported runs only them.
        exclusive = owners is not None and owners <= wake
        selected = []
        for phase in project.phases:
            if phase not in slots:
                woken = (
                    dynamic
                    and not exclusive
                    and (schedule.dynamic_phases is None or phase in schedule.dynamic_phases)
                )
                if fixed or woken:
                    selected.append(phase)
                continue
            behind = schedule.slot_due(phase, now, done.get(phase, frozenset()))
            reported = owners is not None and phase in owners and phase in wake
            if (
                (scheduled_at is not None and schedule.is_slot(phase, scheduled_at))
                or behind
                or reported
            ):
                selected.append(phase)
        return tuple(selected)

    def _owners(self, when) -> frozenset[str] | None:
        """Who reported the server time ``when``; ``None`` when nobody is known."""
        stamp = self.project.schedule.local(when)
        found, unknown = set(), False
        for candidate in self.dynamic_candidates:
            if stamp is not None and abs(candidate - stamp.timestamp()) < SAME_TIME.total_seconds():
                owners = self.dynamic_owners.get(candidate)
                if owners:
                    found |= owners
                else:
                    unknown = True
        return None if unknown or not found else frozenset(found)

    def _slot_view(self, now):
        """The current service day as saved, with today's successful slot runs."""
        view = self.project.new_projection(now)
        self._backfill_slots(view)
        return view

    def effective_schedule(self, now=None):
        """The schedule the next wake follows (idle times while a phase's work is out)."""
        return self._slot_view(now).effective_schedule

    def _backfill_slots(self, view) -> None:
        slots = self.project.schedule.slot_map
        if self.metrics is None or not slots:
            return
        try:
            starts = self.metrics.successful_starts(self.project.name, view.day, slots)
        except Exception:
            return  # metrics never affect a round
        for phase, started in starts:
            view.mark_slot(phase, started)

    def take_overdue(self) -> tuple[float, ...]:
        """Times the last round's phases reported that passed during it, once.

        The service runs the earliest right after the round (see service._late).
        """
        found, self.overdue = self.overdue, ()
        return found

    def resume(self, now=None) -> bool:
        """Take up the saved day after a restart that needs no login (new console code).

        The wakes earlier rounds reported come back from the saved card, which
        is drawn again with the current code. Nothing runs and nobody logs in.
        """
        return self._draw(now)

    def start_day(self, now=None) -> bool:
        """The new service day's card from the manifest alone, without a login.

        For a reset that a round follows soon: that round reads the server.
        """
        return self._draw(now)

    def _draw(self, now) -> bool:
        """Load the saved day and publish it; False when a round holds the project."""
        project = self.project
        now = project.schedule.local(now) or project.schedule.now()
        view = project.new_projection(now)
        self._backfill_slots(view)
        # Shown as planned rather than 读取中; nothing is read or claimed.
        view.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': {}}}})
        self.projection = view
        self._views = {view.day: view}
        self.dynamic_candidates = tuple(view.dynamic_candidates)
        self.dynamic_owners = dict(view.dynamic_owners)
        self.overdue = ()
        try:
            fd = locks.acquire(project.round_lock)
        except locks.AlreadyRunning:
            return False  # a round is running and draws the card itself
        try:
            self._publish(view)
        finally:
            os.close(fd)
        self._flush()
        return True

    def run_once(
        self, *, observe_only=False, phase=None, now=None, phases=None, manual=False
    ) -> dict:
        """Run one round; ``manual`` marks a round started by hand (``once``)."""
        project = self.project
        now = now or project.schedule.now()
        started = datetime.now(project.schedule.tz)
        self._manual = bool(manual)
        fd = locks.acquire(project.round_lock)
        try:
            result = self._run_locked(
                observe_only=observe_only, phase=phase, now=now, phases=phases
            )
        finally:
            os.close(fd)
        # Network delivery does not keep the round lock.
        result['delivery'] = self._flush()
        finished = datetime.now(project.schedule.tz)
        self._record('record_round', {
            'project': project.name,
            'round_id': self.round_id,
            'service_day': result.get('service_day', self.projection.day),
            'mode': 'observe' if observe_only else 'execute',
            'started_at': started.isoformat(timespec='seconds'),
            'finished_at': finished.isoformat(timespec='seconds'),
            'duration_ms': int((finished - started).total_seconds() * 1000),
            'code': result.get('code'),
            'status': result.get('status'),
            'phases': result.get('phases', 0),
            'delivery_status': result['delivery'].get('status'),
            'delivery_error': result['delivery'].get('error_code'),
            'initial_delivery_ms': result.get('initial_delivery_ms'),
        })  # fmt: skip
        return result

    def _run_locked(self, *, observe_only, phase, now, phases=None) -> dict:
        project = self.project
        account = project.account_lock
        if account is not None and not locks.is_free(account):
            # Another program uses the account: never open a second session.
            return {'status': 'busy', 'code': 2}
        self.round_id = uuid.uuid4().hex
        # Same-day history may decorate the display. It never changes the phase
        # list, authorization, quotas or the server reads below.
        self.projection = project.new_projection(now)
        self.projection.set_manual(getattr(self, '_manual', False))
        self._backfill_slots(self.projection)
        if phase:
            phases = (phase,)
        elif phases is None:
            phases = tuple(project.phases)
        self._round = () if observe_only else tuple(phases)
        self.projection.set_round(self._round)
        self._views = {self.projection.day: self.projection}
        self.dynamic_candidates = ()
        self.dynamic_owners = {}
        self.overdue = ()
        target_day = self.projection.day
        self.projection.apply_event(
            {'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': {}}}}
        )
        if observe_only:
            self.projection.apply_event({'kind': 'phase_started', 'phase': project.status_phase})
        self._publish()
        clock = time.monotonic()
        initial_delivery = self._flush()
        initial_ms = int((time.monotonic() - clock) * 1000)
        code, count = 0, 0
        status = 'observed' if observe_only else 'finished'
        try:
            if observe_only:
                code, status = self._observe(target_day)
                count = 1
            else:
                code, count = self._run_phases(target_day, self._round)
        finally:
            for view in self._views.values():
                view.finish_run()
                self._publish(view)
                self._record_task_days(view)
        self.dynamic_candidates = tuple(self.projection.dynamic_candidates)
        self.dynamic_owners = dict(self.projection.dynamic_owners)
        late = {} if observe_only else self.projection.overdue_wakes()
        if late:
            # Kept among the candidates so phases_for knows who owns them.
            self.overdue = tuple(sorted(late))
            self.dynamic_candidates = tuple(sorted({*self.dynamic_candidates, *late}))
            self.dynamic_owners.update({stamp: found for stamp, found in late.items() if found})
        return {
            'status': status,
            'code': code,
            'service_day': self.projection.day,
            'phases': count,
            'initial_delivery': initial_delivery,
            'initial_delivery_ms': initial_ms,
        }

    def _record_task_days(self, view) -> None:
        snapshot = view.snapshot()
        try:  # the card's own count of the day; metrics never affect a round
            verdicts = self.project.task_verdicts(snapshot)
        except Exception:
            verdicts = None
        rows = {
            row['task_id']: {
                'status': row['status'],
                'done': None if verdicts is None else verdicts.get(row['task_id'], -1),
                'fail_streak': row.get('fail_streak'),
            }
            for row in snapshot['tasks']
            if row['level'] != 'step'
        }
        self._record('record_task_days', self.project.name, view.day, rows,
                     snapshot.get('observed_at') or datetime.now().isoformat(timespec='seconds'))  # fmt: skip

    def _observe(self, day) -> tuple[int, str]:
        project = self.project
        self._events = 0
        try:
            code, record = project.observe(day, on_event=self._event, timeout=self.observe_timeout,
                                           round_id=self.round_id)  # fmt: skip
        except (OSError, subprocess.TimeoutExpired):
            code, record = 1, {'execution': 'failed', 'failure_stage': 'connect'}
        # Discovery is read-only: an unknown it reports is never evidence that
        # a state observed earlier today was undone.
        for view in self._views.values():
            view.keep_known()
        self._record(
            'record_phase',
            self._phase_row(project.status_phase, record, 'observe', self._events, self.projection),
        )
        if project.is_busy(record):
            # A manual command took the account after our probe: yield.
            self._event({'kind': 'phase_finished', 'phase': project.status_phase,
                         'status': 'not_executed', 'reason': 'runtime_lock'})  # fmt: skip
            return 2, 'busy'
        self._event(
            {
                'kind': 'phase_finished',
                'phase': project.status_phase,
                'status': 'failed' if code else 'complete',
                'reason': record.get('failure_stage', 'connect') if code else None,
            }
        )
        return code, 'observed'

    def _run_phases(self, day, names) -> tuple[int, int]:
        project = self.project
        scope = project.new_run_scope()
        state = {'code': 0, 'count': 0, 'busy': False}
        names = tuple(names)
        try:
            shared = len(names) > 1 and project.shares_session()
        except Exception:
            shared = False
        if shared:
            names = self._run_shared(day, names, scope, state)
        for name in names:
            if state['busy']:
                break
            self._start_phase(name, state)
            try:
                record = project.run_phase(
                    name, day, run_scope=scope, on_event=self._event, round_id=self.round_id
                )
            except Exception:
                # One broken phase never skips or replays the independent ones.
                record = {'command': name, 'execution': 'failed', 'failure_stage': 'execute',
                          'error_type': 'RunnerError', 'exit_code': None}  # fmt: skip
            self._finish_phase(name, record, state)
        return state['code'], state['count']

    def _run_shared(self, day, names, scope, state) -> tuple:
        """Run the round in one worker that shares its logins.

        Each phase is finished on the card and in the metrics as soon as the
        worker reports it. Returns the phases it never reached; they then run
        on their own, as before. A phase that may have started is always
        reported by the worker side, so nothing is replayed.
        """
        pending = list(names)

        def on_phase(name, record):
            if state['busy'] or not pending or name != pending[0]:
                return
            pending.pop(0)
            self._finish_phase(name, record, state)
            if pending and not state['busy']:
                self._start_phase(pending[0], state)

        self._start_phase(pending[0], state)
        try:
            self.project.run_round(
                names, day, run_scope=scope, on_event=self._event, on_phase=on_phase,
                round_id=self.round_id,
            )  # fmt: skip
        except Exception:
            # The phase in flight may have sent requests: report it, never rerun it.
            if pending and not state['busy']:
                on_phase(pending[0], {'command': pending[0], 'execution': 'failed',
                                      'failure_stage': 'execute', 'error_type': 'RunnerError',
                                      'exit_code': None})  # fmt: skip
        if state['busy']:
            return ()
        if pending:
            # The worker reports every phase it began, so these never started
            # there; the first was only marked started here.
            state['count'] -= 1
        return tuple(pending)

    def _sent_nothing(self, record) -> bool:
        """The phase's own metrics show it sent no request, so it changed nothing."""
        try:
            return self.project.phase_metrics(record).get('actions_sent') == 0
        except Exception:
            return False

    def _start_phase(self, name, state) -> None:
        self._event({'kind': 'phase_started', 'phase': name})
        self._events = 0
        state['count'] += 1

    def _finish_phase(self, name, record, state) -> None:
        project = self.project
        view = self._view(record.get('game_day'))
        view.apply_result(project.result_facts(record))
        if self._sent_nothing(record):
            view.keep_known()
        self._publish(view)
        self._record('record_phase', self._phase_row(name, record, 'execute', self._events, view))
        try:
            code = project.exit_code(record)
        except Exception:
            code = 1
        state['code'] = state['code'] or code
        if (
            code == 0
            and name in project.schedule.slot_map
            and record.get('execution') not in ('awaiting_refresh', 'already_running')
        ):
            # A successful run covers the slot window it started in.
            view.mark_slot(name, record.get('started_at') or project.schedule.now())
            self._publish(view)  # fmt: skip
        elif code and name in project.schedule.slot_map and not project.is_busy(record):
            view.mark_slot(name, record.get('started_at') or project.schedule.now(), failed=True)
            self._publish(view)
        if project.is_busy(record):
            # Yield to a manual command instead of competing for the account.
            state['busy'] = True
