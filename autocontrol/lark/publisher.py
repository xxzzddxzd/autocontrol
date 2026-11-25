"""Best-effort, latest-only delivery of one card per (project, service day, chat).

Execution never waits on, or fails because of, notifications: :meth:`submit`
only keeps the newest snapshot per day in memory. One daemon thread lazily
loads the private bot config and SQLite bindings (reloading them when the
config file changes), renders, and creates or updates the day's card. After a
failure only the newest snapshot of each day is retried, with backoff.

Card identity is made idempotent with a stable per-day UUID: an uncertain
create is resolved from chat history or the same UUID inside the server's
one-hour idempotency window, never by sending a second card.

The console-wide switch ``cards.json`` (``{"lark": false}``) turns Lark cards
off: each snapshot is then only saved locally for the web board, and no Feishu
API is called. Turning it back on brings every remembered day to its card.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid

from . import assets
from .client import DeliveryError, FeishuClient, load_config
from .store import BoardStore

UUID_WINDOW_SECONDS = 3500
SUCCESS = frozenset({'sent', 'unchanged', 'stale_ignored', 'local'})
CARDS_FILE = 'cards.json'


def cards_switch_path(console_dir=None) -> Path:
    """The console-wide card switch, next to the project list."""
    if console_dir is None:
        from .. import registry

        console_dir = registry.default_path().parent
    return Path(console_dir) / CARDS_FILE


def _switch_off(path) -> bool:
    try:
        with open(path, 'rb') as source:
            data = json.loads(source.read(4096))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get('lark') is False


def cards_off(console_dir=None) -> bool:
    """True when Lark cards are switched off and snapshots only feed the web board."""
    return _switch_off(cards_switch_path(console_dir))


def set_cards(on: bool, console_dir=None) -> Path:
    """Switch Lark cards on or off for every project; schedulers notice within seconds."""
    path = cards_switch_path(console_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump({'lark': bool(on)}, output)
        output.write('\n')
    os.replace(temporary, path)
    return path


def _observed_at(snapshot):
    value = snapshot.get('observed_at') if isinstance(snapshot, dict) else None
    if not isinstance(value, str):
        return None
    try:
        value = datetime.fromisoformat(value)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def is_older(snapshot, previous) -> bool:
    """True when ``snapshot`` is an out-of-order older display than ``previous``."""
    if not isinstance(previous, dict):
        return False
    incoming, existing = snapshot.get('display_revision'), previous.get('display_revision')
    if type(incoming) is int and type(existing) is int:
        return incoming < existing
    incoming, existing = _observed_at(snapshot), _observed_at(previous)
    return incoming is not None and existing is not None and incoming < existing


@contextmanager
def delivery_lock(directory):
    """Serialize card delivery across daemon and manual processes (transport only)."""
    fd = os.open(
        Path(directory) / '.daily-board-publish.lock',
        os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0),
        0o600,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError('invalid_notification_lock')
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class CardPublisher:
    def __init__(
        self,
        private,
        render,
        sanitize,
        *,
        config_name='lark_app.json',
        config_path=None,
        db_name='daily_board.sqlite3',
        min_interval=3.0,
        retry_interval=30.0,
        max_retry_interval=900.0,
        max_days=32,
        client_factory=FeishuClient,
        uuid_namespace='yslzm-daily-board',
        switch_path=None,
    ):
        self.private = Path(private).expanduser()
        # None: no console-wide switch (cards always go to Lark).
        self.switch_path = Path(switch_path).expanduser() if switch_path else None
        self.render = render
        self.sanitize = sanitize
        self.config_path = (
            Path(config_path).expanduser() if config_path else self.private / config_name
        )
        self.db_path = self.private / db_name
        self.min_interval = max(0.0, float(min_interval))
        self.retry_interval = max(0.01, float(retry_interval))
        self.max_retry_interval = max(self.retry_interval, float(max_retry_interval))
        self.max_days = max(1, int(max_days))
        self.client_factory = client_factory
        self.uuid_namespace = uuid_namespace
        self._cond = threading.Condition()
        self._latest: OrderedDict = OrderedDict()  # newest snapshot per day (re-offered on retry)
        self._pending: OrderedDict = OrderedDict()  # days awaiting an attempt
        self._failed: set = set()  # days whose newest snapshot is not delivered yet
        self._last_attempt: dict = {}
        self._failures = 0
        self._retry_at = 0.0
        self._failed_signature = None
        self._force = False
        self._busy = False
        self._stopping = False
        self._backend = None  # (config signature, store, client, chat_id)
        self._icons = None  # uploaded card images for the current bot
        self._icons_retry_at = 0.0
        self._status = {'status': 'initializing', 'error_code': None, 'api_code': None}
        self._thread = threading.Thread(target=self._work, name='autocontrol-card', daemon=True)
        self._thread.start()

    # -- public API --------------------------------------------------------

    @property
    def status(self) -> dict:
        with self._cond:
            return {**self._status, 'pending': len(self._pending), 'busy': self._busy}

    def submit(self, snapshot) -> bool:
        """Remember the newest display for its day. Never blocks, never raises."""
        try:
            clean = self.sanitize(snapshot)
            key = clean['project'], clean['service_day']
            if not all(isinstance(value, str) and value for value in key):
                return False
        except Exception:
            return False
        with self._cond:
            if self._stopping:
                return False
            self._latest[key] = clean
            self._latest.move_to_end(key)
            self._pending[key] = clean
            # A long outage must not grow memory: this is a display cache, not
            # a business recovery queue.
            while len(self._latest) > self.max_days:
                oldest, _ = self._latest.popitem(last=False)
                self._pending.pop(oldest, None)
                self._failed.discard(oldest)
            self._cond.notify_all()
        return True

    def flush(self, timeout=30.0) -> bool:
        """Attempt everything queued now, bypassing throttle and backoff.

        Returns True once every queued day has been attempted within the
        timeout; :attr:`status` tells whether the last attempt succeeded.
        """
        try:
            deadline = time.monotonic() + max(0.0, float(timeout))
            with self._cond:
                self._force = True
                self._cond.notify_all()
                while self._pending or self._busy:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(min(remaining, 0.5))
                return True
        except Exception:
            return False

    @property
    def delivered(self) -> bool:
        """True when the newest display of every remembered day is on the card."""
        with self._cond:
            return not self._failed and not self._pending and self._status['status'] in SUCCESS

    def close(self, timeout=30.0) -> bool:
        try:
            deadline = time.monotonic() + max(0.0, float(timeout))
            delivered = self.flush(timeout)
            with self._cond:
                self._stopping = True
                self._cond.notify_all()
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
            return delivered and not self._thread.is_alive()
        except Exception:
            return False

    # -- worker thread -------------------------------------------------------

    def _set_status(self, status, error_code=None, api_code=None):
        with self._cond:
            self._status = {'status': status, 'error_code': error_code, 'api_code': api_code}

    def _next_job(self):
        """Wait for the next (key, snapshot) to deliver; ``None`` when stopping."""
        with self._cond:
            while True:
                now = time.monotonic()
                if self._stopping and not self._pending:
                    return None
                if self._retry_at > now and self._config_signature() != self._failed_signature:
                    # The bot config was edited during a backoff: try at once.
                    self._retry_at, self._failures, now = 0.0, 0, time.monotonic()
                if not self._pending and self._failed and now >= self._retry_at:
                    # Re-offer only each day's newest display, never old progress.
                    self._failed &= set(self._latest)
                    for key in self._failed:
                        self._pending.setdefault(key, self._latest[key])
                if self._pending:
                    key = next(iter(self._pending))
                    wait = 0.0
                    if not self._force and not self._stopping:
                        wait = max(
                            self._retry_at - now,
                            self.min_interval - (now - self._last_attempt.get(key, float('-inf'))),
                        )
                    if wait <= 0:
                        snapshot = self._pending.pop(key)
                        self._busy = True
                        if not self._pending:
                            self._force = False
                        return key, snapshot
                    self._cond.wait(min(wait, 5.0))
                    continue
                self._cond.wait(min(5.0, max(0.05, self._retry_at - now)) if self._failed else None)

    def _work(self):
        while True:
            job = self._next_job()
            if job is None:
                return
            key, snapshot = job
            delivered = False
            try:
                delivered = self._deliver(snapshot)
            except Exception:
                # Nothing from rendering, SQLite or the network may reach the
                # business execution path or print diagnostics.
                self._set_status('failed', 'publisher_error')
            finally:
                with self._cond:
                    self._busy = False
                    self._last_attempt[key] = time.monotonic()
                    if delivered:
                        self._failed.discard(key)
                        if not self._failed:
                            self._failures = 0
                    else:
                        self._failed.add(key)
                        self._failed_signature = self._config_signature()
                        self._failures += 1
                        backoff = self.retry_interval * 2 ** min(self._failures - 1, 6)
                        self._retry_at = time.monotonic() + min(backoff, self.max_retry_interval)
                    self._cond.notify_all()

    @staticmethod
    def _file_signature(path):
        if path is None:
            return None
        try:
            info = path.stat()
            return info.st_mtime_ns, info.st_size, info.st_ino
        except OSError:
            return None

    def _config_signature(self):
        config = self._file_signature(self.config_path)
        if self.switch_path is None:
            return config
        return config, self._file_signature(self.switch_path)

    def _local_only(self) -> bool:
        return self.switch_path is not None and _switch_off(self.switch_path)

    def _ensure_backend(self):
        signature = self._config_signature()
        backend = self._backend
        if backend is not None and backend[0] == signature:
            return backend[1:]
        self._backend = None
        if self._local_only():
            # Cards are off: keep each snapshot for the web board under the
            # usual chat key (so turning cards back on finds the same card).
            try:
                chat_id = load_config(self.config_path).get('chat_id') or 'local'
            except DeliveryError:
                chat_id = 'local'
            store = BoardStore(self.db_path, sanitize=self.sanitize)
            self._backend = (signature, store, None, chat_id)
            if backend is not None:
                with self._cond:
                    for key, value in self._latest.items():
                        self._pending.setdefault(key, value)
            return self._backend[1:]
        config = load_config(self.config_path)
        if not config.get('chat_id'):
            raise DeliveryError('chat_id_missing')
        store = BoardStore(self.db_path, sanitize=self.sanitize)
        client = self.client_factory(config)
        self._backend = (signature, store, client, config['chat_id'])
        self._icons = None  # a new bot needs its own uploads
        if backend is not None:
            # A new chat or app: bring every remembered day to the new target.
            with self._cond:
                for key, value in self._latest.items():
                    self._pending.setdefault(key, value)
        return self._backend[1:]

    def _deliver(self, snapshot) -> bool:
        try:
            store, client, chat_id = self._ensure_backend()
        except DeliveryError as exc:
            self._set_status('failed', exc.code, exc.api_code)
            return False
        except Exception:
            self._set_status('failed', 'storage_error')
            return False
        try:
            with delivery_lock(store.path.parent):
                return self._publish(store, client, chat_id, snapshot)
        except Exception:
            self._set_status('failed', 'storage_error')
            return False

    def _card_icons(self, client) -> dict:
        """Uploaded images for the card; an upload failure falls back to static icons."""
        now = time.monotonic()
        if self._icons is not None and (self._icons or now < self._icons_retry_at):
            return self._icons
        try:
            self._icons = {'spinner': assets.spinner_key(self.private, client)}
        except Exception:
            self._icons, self._icons_retry_at = {}, now + 3600.0
        return self._icons

    def _keep_local(self, store, chat_id, snapshot) -> bool:
        """Cards are off: save the newest display for the web board, call nothing."""
        project, day = snapshot['project'], snapshot['service_day']
        row = store.get(project, day, chat_id) or {}
        if is_older(snapshot, row.get('snapshot')):
            self._set_status('stale_ignored')
            return True
        store.save(
            project, day, chat_id, snapshot=snapshot, delivery_status='local', error_code=None
        )
        self._set_status('local')
        return True

    def _publish(self, store, client, chat_id, snapshot) -> bool:
        if client is None:
            return self._keep_local(store, chat_id, snapshot)
        project, day = snapshot['project'], snapshot['service_day']
        icons = self._card_icons(client)
        try:
            card = self.render(snapshot, icons=icons) if icons else self.render(snapshot)
            canonical = json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        except Exception:
            # Retrying the same snapshot cannot help; the next one may.
            self._set_status('failed', 'invalid_card')
            return True
        message_id = None
        recovering = False
        try:
            row = store.get(project, day, chat_id) or {}
            if is_older(snapshot, row.get('snapshot')):
                self._set_status('stale_ignored')
                return True
            message_id = row.get('message_id')
            revision = int(row.get('revision') or 0) + 1
            if message_id and row.get('content_hash') == digest:
                store.save(
                    project,
                    day,
                    chat_id,
                    snapshot=snapshot,
                    delivery_status='unchanged',
                    error_code=None,
                )
                self._set_status('unchanged')
                return True
            stable_uuid = row.get('create_uuid') or str(
                uuid.uuid5(uuid.NAMESPACE_URL, f'{self.uuid_namespace}/{project}/{day}/{chat_id}')
            )
            started = row.get('create_started_at')
            recovering = bool(
                not message_id
                and started
                and row.get('delivery_status') in ('creating', 'create_uncertain')
            )
            if recovering:
                lookup_error = None
                find_card = getattr(client, 'find_card', None)
                if find_card is not None:
                    try:
                        message_id = find_card(chat_id, card, since=started)
                    except DeliveryError as exc:
                        lookup_error = exc
                if message_id:
                    store.save(
                        project,
                        day,
                        chat_id,
                        message_id=message_id,
                        delivery_status='recovered',
                        error_code=None,
                    )
                elif time.time() - started >= UUID_WINDOW_SECONDS:
                    # The UUID window has passed and history did not show the
                    # card: never risk a duplicate by creating another one.
                    reason = lookup_error.code if lookup_error else 'create_result_unavailable'
                    store.save(
                        project,
                        day,
                        chat_id,
                        snapshot=snapshot,
                        delivery_status='create_uncertain',
                        error_code=reason,
                    )
                    self._set_status('uncertain', reason, getattr(lookup_error, 'api_code', None))
                    return False
            if message_id:
                store.save(
                    project,
                    day,
                    chat_id,
                    snapshot=snapshot,
                    delivery_status='updating',
                    error_code=None,
                )
                client.update_card(message_id, card)
            else:
                if row.get('delivery_status') == 'failed':
                    started = None
                started = started or time.time()
                # Commit the stable identity before the network call, so a crash
                # can recover from history or replay this exact UUID.
                store.save(
                    project, day, chat_id, create_uuid=stable_uuid, create_started_at=started,
                    delivery_status='creating', snapshot=snapshot, error_code=None,
                )  # fmt: skip
                try:
                    message_id = client.send_card(chat_id, card, stable_uuid)
                except DeliveryError as exc:
                    state = 'create_uncertain' if exc.uncertain or recovering else 'failed'
                    store.save(
                        project,
                        day,
                        chat_id,
                        snapshot=snapshot,
                        delivery_status=state,
                        error_code=exc.code,
                    )
                    raise
                if recovering:
                    # A UUID replay may return the original message without this
                    # newer content: persist the identity, then PATCH. If the
                    # PATCH fails, later attempts only PATCH.
                    store.save(
                        project,
                        day,
                        chat_id,
                        message_id=message_id,
                        delivery_status='recovered',
                        error_code=None,
                    )
                    client.update_card(message_id, card)
            store.save(
                project, day, chat_id, message_id=message_id, create_uuid=stable_uuid,
                create_started_at=started, content_hash=digest, snapshot=snapshot,
                delivery_status='sent', error_code=None, revision=revision,
            )  # fmt: skip
            self._set_status('sent')
            return True
        except DeliveryError as exc:
            uncertain = exc.uncertain or (not message_id and recovering)
            try:
                state = (
                    ('update_uncertain' if message_id else 'create_uncertain')
                    if uncertain
                    else 'failed'
                )
                store.save(
                    project,
                    day,
                    chat_id,
                    snapshot=snapshot,
                    delivery_status=state,
                    error_code=exc.code,
                )
            except Exception:
                pass
            self._set_status('uncertain' if uncertain else 'failed', exc.code, exc.api_code)
            return False
        except Exception:
            self._set_status('failed', 'storage_error')
            return False
