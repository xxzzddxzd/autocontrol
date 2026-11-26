"""Private SQLite storage for card bindings and sanitized display snapshots.

Nothing here is an execution ledger: workers obtain eligibility, counters and
progress from the game server on every connection. The schema is the one the
earlier ``tools/daily_board`` used, so existing bindings keep working.
"""

from __future__ import annotations

from datetime import date
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

_TEXT_KEYS = frozenset(
    {
        'project', 'service_day', 'timezone', 'reset_at', 'updated_at', 'observed_at',
        'next_check_at', 'next_run_at', 'next_discovery_at', 'status', 'phase', 'task_id',
        'title', 'reason', 'unit', 'availability', 'opens_at', 'ends_at', 'finished_at',
        'parent_task', 'done_at', 'done_how', 'trouble',
    }
)  # fmt: skip
_NUMBER_KEYS = frozenset(
    {'schema', 'current', 'total', 'generation', 'sequence', 'display_revision', 'fail_streak'}
)
_BOOL_KEYS = frozenset({'discovered', 'running', 'seen', 'opened'})
_SUMMARY_KEYS = frozenset(
    {
        'completed', 'running', 'waiting', 'failed', 'not_started', 'blocked', 'unavailable',
        'incomplete', 'total', 'unsupported', 'complete', 'result_unavailable', 'closed',
    }
)  # fmt: skip
_SENSITIVE = re.compile(r'(?i)(?:secret|token|password|authorization|session_key|webhook)')
_URL = re.compile(r'https?://\S+')
_ISO = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)')
_COLUMN_KEYS = frozenset(
    {
        'message_id',
        'content_hash',
        'create_uuid',
        'create_started_at',
        'delivery_status',
        'error_code',
        'revision',
    }
)
_METADATA = re.compile(r'[a-zA-Z0-9_.:-]{1,128}')


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def sanitize_snapshot(snapshot, *, count_keys=frozenset(), phases=frozenset()) -> dict:
    """Keep only bounded display fields, never reports, responses or secrets."""
    if not isinstance(snapshot, dict):
        raise ValueError('invalid_snapshot')

    def clean(obj, *, task=False):
        result = {}
        for key, value in obj.items():
            if key in _TEXT_KEYS and isinstance(value, str):
                if _SENSITIVE.search(value):
                    result[key] = '[敏感内容已隐藏]'
                else:
                    result[key] = _URL.sub('[链接已隐藏]', value)[:512]
            elif key in _NUMBER_KEYS and _number(value):
                result[key] = value
            elif key in _BOOL_KEYS and isinstance(value, bool):
                result[key] = value
            elif key == 'level' and task and value in ('goal', 'job', 'step'):
                result[key] = value
            elif key == 'counts' and task and isinstance(value, dict):
                result[key] = {
                    k: v
                    for k, v in list(value.items())[:64]
                    if isinstance(k, str)
                    and k in count_keys
                    and not _SENSITIVE.search(k)
                    and _number(v)
                }
        return result

    result = clean(snapshot)
    for key in ('tasks', 'phases'):
        values = snapshot.get(key)
        if isinstance(values, list):
            result[key] = [
                clean(item, task=True) if isinstance(item, dict) else item
                for item in values[:256]
                if isinstance(item, dict)
                or key == 'phases'
                and isinstance(item, str)
                and item in phases
            ]
    order = snapshot.get('round_phases')
    if isinstance(order, list):
        result['round_phases'] = [
            value for value in order[:16] if isinstance(value, str) and value in phases
        ]
    for key in ('slot_runs', 'slot_fails'):
        runs = snapshot.get(key)
        if isinstance(runs, dict):
            result[key] = {
                phase: sorted(
                    {index for index in indexes[:48] if type(index) is int and 0 <= index < 48}
                )
                for phase, indexes in list(runs.items())[:16]
                if isinstance(phase, str) and phase in phases and isinstance(indexes, list)
            }
    wakes = snapshot.get('dynamic_candidates')
    if isinstance(wakes, list):
        result['dynamic_candidates'] = [
            value for value in wakes[:48] if isinstance(value, str) and _ISO.fullmatch(value)
        ]
    owners = snapshot.get('dynamic_owners')
    if isinstance(owners, dict):
        result['dynamic_owners'] = {
            stamp: [name for name in found[:8] if name == '*' or name in phases]
            for stamp, found in list(owners.items())[:48]
            if isinstance(stamp, str) and _ISO.fullmatch(stamp) and isinstance(found, list)
        }
    summary = snapshot.get('summary')
    if isinstance(summary, dict):
        result['summary'] = {k: v for k, v in summary.items() if k in _SUMMARY_KEYS and _number(v)}
    return result


class BoardStore:
    """Thread-safe card bindings and the latest sanitized snapshot per day."""

    def __init__(self, path, *, sanitize=sanitize_snapshot):
        self.path = Path(path).expanduser()
        self._sanitize = sanitize
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Pre-create the database privately; SQLite's journal inherits its mode.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connect() as conn:
            conn.execute(
                '''CREATE TABLE IF NOT EXISTS boards (
                project TEXT NOT NULL,
                service_day TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                message_id TEXT,
                content_hash TEXT,
                snapshot_json TEXT,
                create_uuid TEXT,
                create_started_at REAL,
                delivery_status TEXT NOT NULL DEFAULT 'not_sent',
                error_code TEXT,
                revision INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (project, service_day, chat_id)
            )'''
            )

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _identity(project, service_day, chat_id):
        if not isinstance(project, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', project):
            raise ValueError('invalid_project')
        if (
            not isinstance(service_day, str)
            or date.fromisoformat(service_day).isoformat() != service_day
        ):
            raise ValueError('invalid_service_day')
        if not isinstance(chat_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', chat_id):
            raise ValueError('invalid_chat_id')
        return project, service_day, chat_id

    def get(self, project, service_day, chat_id) -> dict | None:
        identity = self._identity(project, service_day, chat_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM boards WHERE project=? AND service_day=? AND chat_id=?', identity
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        document = result.pop('snapshot_json', None)
        result['snapshot'] = json.loads(document) if document else None
        return result

    def save(self, project, service_day, chat_id, **fields) -> None:
        identity = self._identity(project, service_day, chat_id)
        values = {k: v for k, v in fields.items() if k in _COLUMN_KEYS}
        if 'snapshot' in fields:
            values['snapshot_json'] = json.dumps(
                self._sanitize(fields['snapshot']), ensure_ascii=False, separators=(',', ':')
            )
        for key in ('message_id', 'content_hash', 'create_uuid', 'delivery_status', 'error_code'):
            value = values.get(key)
            if value is not None and (not isinstance(value, str) or not _METADATA.fullmatch(value)):
                raise ValueError('invalid_metadata')
        for key in ('create_started_at', 'revision'):
            if values.get(key) is not None and not _number(values[key]):
                raise ValueError('invalid_metadata')
        values['updated_at'] = time.time()
        keys = list(values)
        columns = ['project', 'service_day', 'chat_id', *keys]
        sql = (
            'INSERT INTO boards (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns)
            + ') ON CONFLICT(project,service_day,chat_id) DO UPDATE SET '
            + ','.join(f'{key}=excluded.{key}' for key in keys)
        )  # fmt: skip
        with self._lock, self._connect() as conn:
            conn.execute(sql, (*identity, *(values[k] for k in keys)))

    @staticmethod
    def latest_snapshot(path, project, service_day):
        """Read-only lookup of the newest saved display for a day (or ``None``)."""
        try:
            uri = Path(path).expanduser().as_uri() + '?mode=ro'
            with sqlite3.connect(uri, uri=True, timeout=0.1) as db:
                row = db.execute(
                    'SELECT snapshot_json FROM boards WHERE project=? AND service_day=? '
                    'ORDER BY updated_at DESC LIMIT 1',
                    (project, service_day),
                ).fetchone()
            return json.loads(row[0]) if row and row[0] else None
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return None
