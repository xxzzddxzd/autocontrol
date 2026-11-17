"""Operational metrics: what happened in each phase and round, and how healthy a project is.

Recorded in ``<private>/metrics.sqlite3`` with fixed numeric/enumerated columns
only. Metrics are for people and for admission review; execution never reads
them. Recording failures are swallowed so they can never affect a round.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import math
import os
from pathlib import Path
import sqlite3
import threading

from .projects.base import ACTION_METRICS

PHASE_COLUMNS = (
    'project', 'round_id', 'service_day', 'phase', 'mode', 'started_at', 'finished_at',
    'duration_ms', 'exit_code', 'execution', 'failure_stage', 'error_type', 'timed_out', 'busy',
    'tasks_total', 'tasks_complete', 'tasks_failed', 'tasks_unavailable', 'tasks_blocked',
    'tasks_waiting', *ACTION_METRICS, 'events', 'events_rejected', 'protocol_violations',
    'result_valid',
)  # fmt: skip
ROUND_COLUMNS = (
    'project', 'round_id', 'service_day', 'mode', 'started_at', 'finished_at', 'duration_ms',
    'code', 'status', 'phases', 'delivery_status', 'delivery_error', 'initial_delivery_ms',
)  # fmt: skip
INTEGER_COLUMNS = frozenset(
    ('duration_ms', 'exit_code', 'timed_out', 'busy', 'tasks_total', 'tasks_complete', 'tasks_failed',
     'tasks_unavailable', 'tasks_blocked', 'tasks_waiting', *ACTION_METRICS, 'events',
     'events_rejected', 'protocol_violations', 'result_valid', 'code', 'phases', 'initial_delivery_ms')
)  # fmt: skip
FAILED_EXECUTIONS = frozenset(('failed', 'timeout', 'unverified'))
# Admission and operation targets ("各项指标" thresholds), see docs/接入规范.md.
THRESHOLDS = {
    'phase_success_rate': ('>=', 0.95, '阶段成功率'),
    'timeout_rate': ('<=', 0.02, '阶段超时率'),
    'protocol_violations': ('<=', 0, '协议违规次数'),
    'p95_duration_ratio': ('<=', 0.8, 'P95 耗时 / 超时上限'),
    'unconfirmed_action_rate': ('<=', 0.01, '未确认请求占比'),
    'delivery_success_rate': ('>=', 0.99, '卡片送达率'),
    'consecutive_failed_rounds': ('<', 3, '连续失败轮次'),
}


def _clean(value):
    if value is None or isinstance(value, bool):
        return int(value) if isinstance(value, bool) else None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:64]
    return None


class MetricsStore:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connect() as db:
            for table, columns in (('phase_runs', PHASE_COLUMNS), ('rounds', ROUND_COLUMNS)):
                spec = ', '.join(
                    f'{name} {"INTEGER" if name in INTEGER_COLUMNS else "TEXT"}' for name in columns
                )
                db.execute(f'CREATE TABLE IF NOT EXISTS {table} ({spec})')
            db.execute(
                'CREATE TABLE IF NOT EXISTS task_days (project TEXT NOT NULL, service_day TEXT NOT NULL, '
                'task_id TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL, '
                'PRIMARY KEY (project, service_day, task_id))'
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _insert(self, table, columns, row):
        values = [_clean(row.get(name)) for name in columns]
        with self._lock, self._connect() as db:
            db.execute(
                f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                values,
            )

    def record_phase(self, row: dict) -> None:
        self._insert('phase_runs', PHASE_COLUMNS, row)

    def record_round(self, row: dict) -> None:
        self._insert('rounds', ROUND_COLUMNS, row)

    def record_task_days(self, project, service_day, statuses: dict, when: str) -> None:
        with self._lock, self._connect() as db:
            db.executemany(
                'INSERT INTO task_days VALUES (?,?,?,?,?) ON CONFLICT(project,service_day,task_id) '
                'DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at',
                [(project, service_day, task, status, when) for task, status in statuses.items()],
            )

    # -- reporting -------------------------------------------------------------

    def successful_starts(self, project: str, service_day: str, phases) -> list[tuple[str, str]]:
        """``(phase, started_at)`` of today's execute runs of ``phases`` that exited 0."""
        phases = tuple(phases)
        if not phases:
            return []
        marks = ','.join('?' * len(phases))
        with self._lock, self._connect() as db:
            return [
                (phase, started)
                for phase, started in db.execute(
                    f'SELECT phase, started_at FROM phase_runs WHERE project=? AND service_day=?'
                    f" AND mode='execute' AND exit_code=0 AND phase IN ({marks})",
                    (project, service_day, *phases),
                )
                if type(phase) is str and type(started) is str
            ]

    def _rows(self, table, project, since):
        with self._lock, self._connect() as db:
            db.row_factory = sqlite3.Row
            column = 'service_day'
            return [
                dict(row)
                for row in db.execute(
                    f'SELECT * FROM {table} WHERE project=? AND {column}>=? ORDER BY rowid',
                    (project, since),
                )
            ]

    def report(self, project, *, days=7, today=None, phase_timeout=None) -> dict:
        """Indicators over the last ``days`` service days, with threshold checks."""
        today = today or datetime.now().date().isoformat()
        since = (datetime.fromisoformat(today) - timedelta(days=days - 1)).date().isoformat()
        phases = self._rows('phase_runs', project, since)
        rounds = self._rows('rounds', project, since)
        tasks = self._rows('task_days', project, since)
        executed = [row for row in phases if row['mode'] == 'execute' and not row['busy']]
        per_phase = {}
        for row in executed:
            per_phase.setdefault(row['phase'], []).append(row)
        phase_stats = {name: _phase_stats(rows, phase_timeout) for name, rows in per_phase.items()}
        overall = _phase_stats(executed, phase_timeout)
        observe = _phase_stats(
            [row for row in phases if row['mode'] == 'observe' and not row['busy']], None
        )
        streak = 0
        for row in reversed(rounds):
            if row['status'] == 'busy':
                continue
            if row['code']:
                streak += 1
            else:
                break
        delivered = [row for row in rounds if row['delivery_status'] is not None]
        ok_rounds = [row for row in rounds if not row['code'] and row['status'] != 'busy']
        task_stats = {}
        for row in tasks:
            if row['status'] == 'closed':
                continue
            entry = task_stats.setdefault(row['task_id'], {'days': 0, 'complete': 0})
            entry['days'] += 1
            entry['complete'] += row['status'] == 'complete'
        for entry in task_stats.values():
            entry['completion_rate'] = round(entry['complete'] / entry['days'], 4)
        indicators = {
            'phase_success_rate': overall['success_rate'],
            'timeout_rate': overall['timeout_rate'],
            'protocol_violations': overall['protocol_violations'],
            'p95_duration_ratio': overall['p95_duration_ratio'],
            'unconfirmed_action_rate': overall['unconfirmed_action_rate'],
            'delivery_success_rate': (
                round(sum(row['delivery_status'] in ('sent', 'unchanged', 'stale_ignored') for row in delivered)
                      / len(delivered), 4) if delivered else None
            ),  # fmt: skip
            'consecutive_failed_rounds': streak,
        }
        checks = []
        for key, (op, target, title) in THRESHOLDS.items():
            value = indicators[key]
            ok = None if value is None else (
                value >= target if op == '>=' else value <= target if op == '<=' else value < target
            )  # fmt: skip
            checks.append(
                {
                    'indicator': key,
                    'title': title,
                    'value': value,
                    'target': f'{op} {target}',
                    'ok': ok,
                }
            )
        return {
            'project': project,
            'days': days,
            'since': since,
            'rounds': len(rounds),
            'last_success_at': ok_rounds[-1]['finished_at'] if ok_rounds else None,
            'phases': phase_stats,
            'overall': overall,
            'observe': observe,
            'tasks': dict(sorted(task_stats.items())),
            'indicators': indicators,
            'checks': checks,
            'healthy': all(check['ok'] is not False for check in checks),
        }


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _phase_stats(rows, timeout):
    count = len(rows)
    durations = [row['duration_ms'] for row in rows if type(row['duration_ms']) is int]
    ok = [
        row for row in rows if row['exit_code'] == 0 and row['execution'] not in FAILED_EXECUTIONS
    ]
    timeouts = sum(1 for row in rows if row['timed_out'])
    sent = sum(row['actions_sent'] or 0 for row in rows)
    unconfirmed = sum(row['actions_unconfirmed'] or 0 for row in rows)
    p95 = _percentile(durations, 0.95)
    return {
        'runs': count,
        'success_rate': round(len(ok) / count, 4) if count else None,
        'timeout_rate': round(timeouts / count, 4) if count else None,
        'avg_ms': int(sum(durations) / len(durations)) if durations else None,
        'p95_ms': p95,
        'p95_duration_ratio': round(p95 / (timeout * 1000), 4)
        if p95 is not None and timeout
        else None,
        'protocol_violations': (
            sum((row['protocol_violations'] or 0) + (row['events_rejected'] or 0) for row in rows)
            if count
            else None
        ),
        'actions_sent': sent,
        'actions_confirmed': sum(row['actions_confirmed'] or 0 for row in rows),
        'actions_unconfirmed': unconfirmed,
        'unconfirmed_action_rate': round(unconfirmed / sent, 4) if sent else None,
        'failure_stages': _histogram(row['failure_stage'] for row in rows if row['failure_stage']),
    }


def _histogram(values):
    result = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items(), key=lambda item: -item[1]))
