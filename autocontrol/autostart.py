"""Start the console at login and keep it running: one launchd agent for autocontrol.

The agent runs ``run.py console`` and nothing else; projects are never added
here, they are registered with the console. launchd restarts the console only
when it fails. A console stopped with SIGTERM, or one that finds another
console already running, stays stopped until ``launchctl kickstart``.
"""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess

LABEL = 'local.autocontrol.console'
# The agent starts with launchd's bare environment; workers still find
# Homebrew tools (dotnet, luajit) on this search path.
SEARCH_PATH = (
    '/opt/homebrew/bin',
    '/opt/homebrew/sbin',
    '/usr/local/bin',
    '/usr/bin',
    '/bin',
    '/usr/sbin',
    '/sbin',
)


def plist_path() -> Path:
    return Path('~/Library/LaunchAgents').expanduser() / f'{LABEL}.plist'


def service() -> str:
    return f'gui/{os.getuid()}/{LABEL}'


def agent(python, entry, *, log) -> dict:
    entry = Path(entry).resolve()
    return {
        'Label': LABEL,
        'ProgramArguments': [str(python), '-B', str(entry), 'console'],
        'WorkingDirectory': str(entry.parent),
        'EnvironmentVariables': {
            'PATH': ':'.join(path for path in SEARCH_PATH if os.path.isdir(path)),
            'PYTHONDONTWRITEBYTECODE': '1',
        },
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 30,
        # On SIGTERM the console waits up to 30 s for idle schedulers to exit.
        'ExitTimeOut': 60,
        'ProcessType': 'Standard',
        'StandardOutPath': '/dev/null',
        'StandardErrorPath': str(log),
    }


def enable(python, entry, *, log, run=subprocess.run) -> Path:
    """Write the agent and load it; launchd starts the console right away."""
    log = Path(log)
    log.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    log.parent.mkdir(mode=0o700, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_bytes(plistlib.dumps(agent(python, entry, log=log)))
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
    run(['launchctl', 'bootout', service()], capture_output=True)  # not loaded yet is fine
    domain = service().rsplit('/', 1)[0]
    run(['launchctl', 'bootstrap', domain, str(path)], check=True, capture_output=True)
    return path


def disable(run=subprocess.run) -> bool:
    """Unload the agent (a running console gets SIGTERM) and delete it."""
    run(['launchctl', 'bootout', service()], capture_output=True)
    path = plist_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def state(run=subprocess.run) -> str | None:
    """``running``, ``not running`` and so on; None when the agent is not loaded."""
    try:
        result = run(['launchctl', 'print', service()], capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        key, _, value = line.strip().partition(' = ')
        if key == 'state':
            return value.strip()
    return 'loaded'
