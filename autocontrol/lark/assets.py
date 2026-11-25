"""Card images uploaded once per bot: the animated running icon.

Image keys belong to the bot that uploaded them, so each private directory
caches ``{"spinner": {"app": <bot digest>, "asset": <file digest>, "key": ...}}``
in ``lark_assets.json`` (0600) and uploads again only when the bot or the
image changes. Nothing secret is stored: the bot is named by a digest.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

SPINNER = Path(__file__).resolve().parents[1] / 'board' / 'assets' / 'spinner.webp'
CACHE_NAME = 'lark_assets.json'


def _read(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as source:
            data = source.read(64 * 1024 + 1)
        value = json.loads(data) if len(data) <= 64 * 1024 else None
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, sort_keys=True)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def spinner_key(private, client) -> str:
    """The bot's image key for the spinner, uploading it on first use."""
    data = SPINNER.read_bytes()
    asset = hashlib.sha256(data).hexdigest()[:16]
    app = client.app_fingerprint
    path = Path(private) / CACHE_NAME
    cache = _read(path)
    entry = cache.get('spinner')
    if (
        isinstance(entry, dict)
        and entry.get('app') == app
        and entry.get('asset') == asset
        and isinstance(entry.get('key'), str)
        and entry['key'].startswith('img_')
    ):
        return entry['key']
    key = client.upload_image(data, SPINNER.name, 'image/webp')
    cache['spinner'] = {'app': app, 'asset': asset, 'key': key}
    try:
        _write(path, cache)
    except OSError:
        pass  # the key still works for this process
    return key
