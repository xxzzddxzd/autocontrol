"""The console's project list: which projects the one console process runs.

A project goes live by being registered here after it passed admission; the
console then starts its scheduler and keeps it running. Removing or disabling
an entry stops that scheduler after its current round. Nothing outside the
console (no crontab line per project) is involved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

CONSOLE_DIR = Path('~/.config/autocontrol/console').expanduser()
VERSION = 1
KEYS = ('name', 'project', 'manifest', 'workdir', 'private', 'python', 'enabled')
NAME = re.compile(r'[a-z][a-z0-9_-]{0,31}')
LEGACY_WRAPPER = {'yslzm'}  # built-in projects whose old entry point is kept working


class RegistryError(ValueError):
    """The project list is unreadable or an entry is invalid."""


def default_path() -> Path:
    return CONSOLE_DIR / 'projects.json'


def _entry(item) -> dict:
    if type(item) is not dict:
        raise RegistryError('invalid project entry')
    name = item.get('name')
    if type(name) is not str or not NAME.fullmatch(name):
        raise RegistryError('invalid project name')
    if (item.get('manifest') is None) == (item.get('project') is None):
        raise RegistryError(f'{name}: give exactly one of manifest / project')
    if item.get('project') is not None and type(item['project']) is not str:
        raise RegistryError(f'{name}: invalid project')
    for key in ('manifest', 'workdir', 'private', 'python'):
        value = item.get(key)
        if value is not None and (type(value) is not str or not os.path.isabs(value)):
            raise RegistryError(f'{name}: {key} must be an absolute path')
    if item.get('private') is None:
        raise RegistryError(f'{name}: private is required')
    enabled = item.get('enabled', True)
    if type(enabled) is not bool:
        raise RegistryError(f'{name}: enabled must be true or false')
    return {key: item[key] for key in KEYS if item.get(key) is not None} | {'enabled': enabled}


def load(path=None) -> list[dict]:
    """Registered projects in order; a missing file is an empty list."""
    path = Path(path) if path else default_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise RegistryError(f'unreadable registry: {type(error).__name__}') from None
    if type(data) is not dict or data.get('version') != VERSION:
        raise RegistryError('unsupported registry version')
    if type(data.get('projects')) is not list:
        raise RegistryError('invalid registry')
    entries, seen = [], set()
    for item in data['projects']:
        entry = _entry(item)
        if entry['name'] in seen:
            raise RegistryError(f'duplicate project {entry["name"]}')
        seen.add(entry['name'])
        entries.append(entry)
    return entries


def save(path, entries) -> None:
    """Replace the list atomically (0600, in a 0700 directory)."""
    path = Path(path) if path else default_path()
    clean = [_entry(entry) for entry in entries]
    names = [entry['name'] for entry in clean]
    if len(set(names)) != len(names):
        raise RegistryError('duplicate project names')
    path.parent.parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name('.' + path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump({'version': VERSION, 'projects': clean}, output, ensure_ascii=False, indent=2)
        output.write('\n')
    os.replace(temporary, path)


def entry_for(project, *, python=None, enabled=True) -> dict:
    """The registry entry that runs ``project`` as loaded by the command line."""
    entry = {'name': project.name, 'private': str(Path(project.private).resolve())}
    if project.manifest_path is not None:
        entry['manifest'] = str(Path(project.manifest_path).resolve())
    else:
        entry['project'] = project.name
    if project.workdir is not None:
        entry['workdir'] = str(Path(project.workdir).resolve())
    if python:
        entry['python'] = str(Path(python).expanduser())
    entry['enabled'] = enabled
    return _entry(entry)


def upsert(path, entry) -> list[dict]:
    """Add the project, or replace the entry of the same name in place."""
    entries = load(path)
    entry = _entry(entry)
    for index, current in enumerate(entries):
        if current['name'] == entry['name']:
            entries[index] = entry
            break
    else:
        entries.append(entry)
    save(path, entries)
    return entries


def remove(path, name) -> bool:
    entries = load(path)
    kept = [entry for entry in entries if entry['name'] != name]
    if len(kept) == len(entries):
        return False
    save(path, kept)
    return True


def legacy_wrapper(entry, project) -> str:
    """Old ``run_scheduled.py <phase> [--full-checklist]`` callers enter the console."""
    return (
        '#!/usr/bin/env python3\nimport runpy, sys\n'
        f'entry = {str(entry)!r}\n'
        'old = sys.argv[1:]\n'
        'command = old[0] if old and not old[0].startswith("-") else "auto"\n'
        f'sys.argv = [entry, "once", "--project", {project.name!r}, '
        f'"--workdir", {str(project.workdir)!r}, "--private", {str(project.private)!r}]\n'
        'if "--full-checklist" not in old: sys.argv += ["--phase", command]\n'
        'runpy.run_path(entry, run_name="__main__")\n'
    )


def write_legacy_wrapper(entry, project) -> None:
    if project.name not in LEGACY_WRAPPER or project.manifest_path is not None:
        return
    descriptor = os.open(
        project.private / 'run_scheduled.py',
        os.O_WRONLY | os.O_TRUNC | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, 'w') as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(legacy_wrapper(entry, project))
