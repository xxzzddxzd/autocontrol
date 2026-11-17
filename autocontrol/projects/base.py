"""What the console needs to know about one automated project.

A project supplies its checklist catalog and schedule, how to run its worker
phases, and how to translate a finished phase into board facts and metrics.
Two kinds exist (see ``docs/接入规范.md``):

* protocol projects (:class:`~autocontrol.projects.manifest.ManifestProject`):
  the script implements worker protocol v1 and is described by a manifest;
* adapter projects (e.g. yslzm): a Python subclass translates a script's
  native output. They must still meet the same admission criteria.
"""

from __future__ import annotations

from functools import partial
import json
from pathlib import Path
import sys

from ..board.card import CardRenderer
from ..board.catalog import Catalog
from ..board.projection import Projection
from ..lark.store import BoardStore, sanitize_snapshot
from ..schedule import Schedule

# Standard operational metrics recorded per phase run (see protocol.METRIC_KEYS).
ACTION_METRICS = (
    'actions_sent',
    'actions_confirmed',
    'actions_failed',
    'actions_unconfirmed',
    'reads',
    'retries',
    'sessions',
)


class Project:
    name: str = ''
    catalog: Catalog
    schedule: Schedule
    phases: tuple[str, ...] = ()  # one full round, in execution order
    commands: tuple[str, ...] = ()  # phases that may be run on their own
    status_phase = 'status'  # read-only discovery phase
    default_workdir: Path
    default_private: Path
    uuid_namespace = 'autocontrol-daily-board'
    phase_timeout: float | None = None
    observe_timeout: float = 180.0
    manifest_path: Path | None = None

    def __init__(self, *, workdir=None, private=None, python=None):
        self.workdir = Path(workdir or self.default_workdir).expanduser()
        self.private = Path(private or self.default_private).expanduser()
        self.python = python or sys.executable
        self.renderer = CardRenderer(self.catalog, self.schedule)
        self.sanitize = partial(
            sanitize_snapshot,
            count_keys=self.catalog.count_keys,
            phases=frozenset(self.catalog.phases),
        )
        if not self.commands:
            self.commands = self.phases

    # -- shared locations (also used by the worker) ----------------------

    @property
    def coordinator_lock(self) -> Path:
        return self.private / 'coordinator.lock'

    @property
    def round_lock(self) -> Path:
        return self.private / 'round.lock'

    @property
    def account_lock(self) -> Path | None:
        """The worker's own account lock, probed (never held) before a round."""
        return self.private / 'state' / 'runtime.lock'

    @property
    def board_db(self) -> Path:
        return self.private / 'daily_board.sqlite3'

    @property
    def metrics_db(self) -> Path:
        return self.private / 'metrics.sqlite3'

    @property
    def lark_config(self) -> Path:
        return self.private / 'lark_app.json'

    # -- board ---------------------------------------------------------------

    def render(self, snapshot) -> dict:
        return self.renderer.render(snapshot)

    def previous_display(self, day: str):
        snapshot = BoardStore.latest_snapshot(self.board_db, self.catalog.project, day)
        try:
            return self.sanitize(snapshot) if snapshot is not None else None
        except ValueError:
            return None

    def new_projection(self, now=None, *, restore=True) -> Projection:
        day = self.schedule.service_day(now)
        previous = self.previous_display(day) if restore else None
        return Projection(self.catalog, self.schedule, now, previous)

    # -- worker hooks ----------------------------------------------------------

    def new_run_scope(self):
        """Per-round, in-memory state handed from phase to phase (or ``None``)."""
        return None

    def observe(self, day: str, *, on_event, timeout: float, round_id=None) -> tuple[int, dict]:
        """Read-only discovery of today's plan: ``(exit code, record)``."""
        raise NotImplementedError

    def run_phase(self, phase: str, day: str, *, run_scope, on_event, round_id=None) -> dict:
        """Run one phase and return its fixed-field record."""
        raise NotImplementedError

    def settings(self) -> dict:
        """Console settings of this project: ``<private>/autocontrol.json``.

        A small JSON object the operator edits (e.g. ``{"shared_session":
        true}``); missing, oversized or malformed files read as ``{}``.
        """
        try:
            raw = (self.private / 'autocontrol.json').read_bytes()
            data = json.loads(raw) if len(raw) <= 64 * 1024 else None
        except (OSError, ValueError):
            return {}
        return data if type(data) is dict else {}

    def shares_session(self) -> bool:
        """Run a whole round in one worker process (see :meth:`run_round`)."""
        return False

    def run_round(self, phases, day: str, *, run_scope, on_event, on_phase, round_id=None):
        """Run ``phases`` in one worker process; call ``on_phase(phase, record)``
        as each ends and return the phases that reported."""
        raise NotImplementedError

    def result_facts(self, record: dict) -> dict:
        """Translate a phase record into :meth:`Projection.apply_result` input."""
        raise NotImplementedError

    def exit_code(self, record: dict) -> int:
        raise NotImplementedError

    def is_busy(self, record: dict) -> bool:
        """The account was in use by another program: stop this round."""
        return record.get('execution') in ('already_running', 'busy')

    def phase_metrics(self, record: dict) -> dict:
        """Standard action metrics of one phase record (non-negative ints)."""
        source = record.get('metrics')
        source = source if isinstance(source, dict) else {}
        return {key: source[key] for key in ACTION_METRICS if type(source.get(key)) is int}

    def protocol_metrics(self, record: dict) -> dict:
        """Protocol conformance counters of one phase record."""
        source = record.get('protocol')
        if not isinstance(source, dict):
            return {}
        return {
            'events_rejected': int(source.get('events_rejected', 0)),
            'protocol_violations': len(source.get('violations', ())),
            'result_valid': bool(source.get('result_valid')),
        }
