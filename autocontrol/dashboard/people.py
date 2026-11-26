"""Who has asked to see the board, and who has been seen: ``board_people.json`` (0600).

``requests`` holds one entry per person turned away: a random id (the
approval link names it, never the open_id), their name, when they asked, when
the owner was last told, and the outcome. ``people`` remembers the name each
permitted person signed in with, so the members page can show names.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import threading
import time

NAME = 'board_people.json'
NOTIFY_EVERY = 24 * 3600.0  # tell the owner about the same person at most once a day
DENIED_QUIET = 7 * 24 * 3600.0  # after a refusal, stay quiet this long
MAX_REQUESTS = 200
MAX_BYTES = 256 * 1024


class People:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            with open(self.path, 'rb') as source:
                data = json.loads(source.read(MAX_BYTES))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        requests = data.get('requests') if isinstance(data.get('requests'), dict) else {}
        people = data.get('people') if isinstance(data.get('people'), dict) else {}
        return {'requests': requests, 'people': people}

    def _write(self, data) -> None:
        requests = data['requests']
        if len(requests) > MAX_REQUESTS:
            keep = sorted(requests, key=lambda key: requests[key].get('asked', 0))[-MAX_REQUESTS:]
            data['requests'] = {key: requests[key] for key in keep}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(f'.{self.path.name}.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write('\n')
        os.replace(temporary, self.path)

    def snapshot(self) -> dict:
        with self._lock:
            return self._read()

    def ask(self, open_id: str, name: str) -> tuple[dict, bool]:
        """Record that someone was turned away: ``(request, tell the owner now?)``."""
        now = self.clock()
        with self._lock:
            data = self._read()
            found = next(
                (dict(item, id=key) for key, item in data['requests'].items()
                 if item.get('open_id') == open_id),
                None,
            )  # fmt: skip
            if found is None:
                key = secrets.token_urlsafe(12)
                found = {'id': key, 'open_id': open_id, 'name': name, 'asked': now,
                         'notified': None, 'status': 'pending', 'decided': None}  # fmt: skip
            key = found.pop('id')
            found['name'] = name or found.get('name') or '未命名'
            found['last'] = now
            if found.get('status') == 'approved':
                found['status'] = 'pending'  # removed again later: asking anew
            if found.get('status') == 'denied':
                decided = found.get('decided')
                tell = decided is None or now - decided >= DENIED_QUIET
                if tell:
                    found['status'] = 'pending'
            else:
                notified = found.get('notified')
                tell = notified is None or now - notified >= NOTIFY_EVERY
            if tell:
                found['notified'] = now
            data['requests'][key] = found
            self._write(data)
        return dict(found, id=key), tell

    def get(self, key):
        with self._lock:
            item = self._read()['requests'].get(key) if isinstance(key, str) else None
        return dict(item, id=key) if isinstance(item, dict) else None

    def decide(self, key, verdict: str) -> dict | None:
        """Mark a request approved or denied; returns it, or None when unknown."""
        if verdict not in ('approved', 'denied'):
            raise ValueError('verdict')
        with self._lock:
            data = self._read()
            item = data['requests'].get(key) if isinstance(key, str) else None
            if not isinstance(item, dict):
                return None
            item.update(status=verdict, decided=self.clock())
            self._write(data)
        return dict(item, id=key)

    def seen(self, open_id: str, name: str) -> None:
        """Remember a permitted person's name (at most every few minutes)."""
        now = self.clock()
        with self._lock:
            data = self._read()
            known = data['people'].get(open_id)
            if (
                isinstance(known, dict)
                and known.get('name') == name
                and now - known.get('seen', 0) < 300
            ):
                return
            data['people'][open_id] = {'name': name, 'seen': now}
            self._write(data)

    def name(self, open_id) -> str | None:
        data = self.snapshot()
        person = data['people'].get(open_id)
        if isinstance(person, dict) and person.get('name'):
            return person['name']
        for item in data['requests'].values():
            if isinstance(item, dict) and item.get('open_id') == open_id and item.get('name'):
                return item['name']
        return None
