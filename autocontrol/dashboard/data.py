"""What the board shows, read from local files only (never the network).

The project list, the console's status file, each project's newest saved
card snapshot (rendered with the project's own card code, so the board says
exactly what the card says), its metrics database and its scheduling log.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading

from .. import locks, registry
from ..lark.store import BoardStore

LOG_TAIL_BYTES = 128 * 1024
LOG_LINES = 80
ROUNDS = 12
DAYS = 14


def _load_project(entry):
    from ..console import load_project

    return load_project(entry)


def _read_only(path):
    uri = Path(path).expanduser().as_uri() + '?mode=ro'
    return sqlite3.connect(uri, uri=True, timeout=0.5)


class Source:
    """Reads the console's files. Safe to share between request threads."""

    def __init__(self, registry_path=None, *, loader=_load_project):
        self.registry_path = Path(registry_path) if registry_path else registry.default_path()
        self.dir = self.registry_path.parent
        self.loader = loader
        self._projects: dict[str, tuple] = {}
        self._lock = threading.Lock()

    # -- the console -------------------------------------------------------------

    def entries(self) -> tuple[list[dict], str | None]:
        try:
            return registry.load(self.registry_path), None
        except registry.RegistryError as error:
            return [], str(error)

    def console(self) -> dict:
        try:
            status = json.loads((self.dir / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            status = {}
        running = not locks.is_free(self.dir / 'console.lock')
        return {'running': running, **(status if isinstance(status, dict) else {})}

    def cards(self) -> dict:
        """Lark cards: ``{'on': switched on, 'paused_until': quota pause end or None}``."""
        from ..lark.publisher import cards_state

        return cards_state(self.dir)

    # -- one project ---------------------------------------------------------------

    def project(self, entry):
        """The project an entry names, loaded once and again when its manifest changes."""
        stamp = None
        if entry.get('manifest'):
            try:
                info = Path(entry['manifest']).stat()
                stamp = (info.st_mtime_ns, info.st_size)
            except OSError:
                stamp = 'missing'
        key = (json.dumps(entry, sort_keys=True, ensure_ascii=False), stamp)
        name = entry['name']
        with self._lock:
            cached = self._projects.get(name)
            if cached is not None and cached[0] == key:
                return cached[1]
        project = self.loader(entry)
        with self._lock:
            self._projects[name] = (key, project)
        return project

    def board(self, entry, day=None, now=None) -> dict:
        """The day's card (all details) and the days that have one."""
        result = {'name': entry['name'], 'title': entry['name'], 'day': day, 'today': None,
                  'days': [], 'card': None, 'error': None, 'project': None}  # fmt: skip
        try:
            project = self.project(entry)
        except Exception as error:
            result['error'] = f'项目加载失败：{type(error).__name__}'
            return result
        result['project'] = project
        result['title'] = getattr(project.catalog, 'display_name', None) or entry['name']
        try:
            today = project.schedule.service_day(now)
        except Exception:
            today = None
        result['today'] = today
        result['day'] = day = day or today
        result['days'] = self.days(project)
        if day is None:
            return result
        snapshot = BoardStore.latest_snapshot(project.board_db, project.catalog.project, day)
        if snapshot is None:
            return result
        try:
            result['card'] = project.render_full(snapshot)
        except Exception as error:
            result['error'] = f'卡片渲染失败：{type(error).__name__}'
            return result
        try:
            verdicts = project.task_verdicts(snapshot).values()
            result['progress'] = (sum(v == 1 for v in verdicts), sum(v >= 0 for v in verdicts))
        except Exception:
            pass  # the bar is optional
        return result

    @staticmethod
    def days(project, limit=DAYS) -> list[str]:
        try:
            with _read_only(project.board_db) as db:
                rows = db.execute(
                    'SELECT DISTINCT service_day FROM boards WHERE project=? '
                    'ORDER BY service_day DESC LIMIT ?',
                    (project.catalog.project, limit),
                ).fetchall()
        except (OSError, sqlite3.Error, ValueError):
            return []
        return [row[0] for row in rows]

    @staticmethod
    def rounds(project, limit=ROUNDS) -> list[dict]:
        try:
            with _read_only(project.metrics_db) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    'SELECT mode, started_at, finished_at, duration_ms, code, status, phases, '
                    'delivery_status FROM rounds ORDER BY started_at DESC LIMIT ?',
                    (limit,),
                ).fetchall()
        except (OSError, sqlite3.Error, ValueError):
            return []
        return [dict(row) for row in rows]

    def log_tail(self, name, lines=LOG_LINES) -> list[str]:
        """The newest lines of the project's scheduling log, oldest first."""
        path = self.dir / 'logs' / f'{name}.log'
        try:
            with open(path, 'rb') as source:
                source.seek(0, 2)
                size = source.tell()
                source.seek(max(0, size - LOG_TAIL_BYTES))
                data = source.read()
        except OSError:
            return []
        text = data.decode('utf-8', 'replace').splitlines()
        if size > LOG_TAIL_BYTES:
            text = text[1:]  # the first line was cut in the middle
        return text[-lines:]


def stamp(value, tz) -> str:
    """``MM-DD HH:MM`` of an ISO timestamp in the board's time zone ('' when unreadable)."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return ''
    if moment.tzinfo is not None:
        moment = moment.astimezone(tz)
    return moment.strftime('%m-%d %H:%M')
