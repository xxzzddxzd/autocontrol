"""Install the cron watchdog that keeps ``autocontrol serve`` running.

Only this project's own block (and the legacy yslzm entries it replaces) is
touched; other crontab lines are preserved verbatim.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

LEGACY_BLOCKS = {'yslzm': ('# BEGIN YSLZM LOCAL DAILY BOARD', '# END YSLZM LOCAL DAILY BOARD')}
LEGACY_MARKERS = {
    'yslzm': ('# yslzm-daily-auto', '# yslzm-cardbook-resume', '# yslzm-cardbook-finalize')
}


def markers(name: str) -> tuple[str, str]:
    return f'# BEGIN AUTOCONTROL {name.upper()}', f'# END AUTOCONTROL {name.upper()}'


def serve_command(python, entry, project) -> list[str]:
    target = (
        ['--manifest', str(project.manifest_path)]
        if project.manifest_path is not None
        else ['--project', project.name]
    )
    return [
        '/usr/bin/caffeinate', '-i', str(python), '-B', str(entry), 'serve', *target,
        '--workdir', str(project.workdir), '--private', str(project.private),
    ]  # fmt: skip


def cron_text(current: str, python, entry, project) -> str:
    begin, end = markers(project.name)
    blocks = [(begin, end)]
    if project.name in LEGACY_BLOCKS:
        blocks.append(LEGACY_BLOCKS[project.name])
    old_markers = LEGACY_MARKERS.get(project.name, ())
    kept, inside = [], None
    for line in current.splitlines():
        if inside is None:
            opening = next((pair for pair in blocks if line == pair[0]), None)
            if opening is not None:
                inside = opening[1]
                continue
            if any(marker in line for marker in old_markers):
                continue
            kept.append(line)
        elif line == inside:
            inside = None
    command = ' '.join(shlex.quote(part) for part in serve_command(python, entry, project))
    line = (
        f'*/5 * * * * /usr/bin/env PYTHONDONTWRITEBYTECODE=1 TZ={project.schedule.tz_name} '
        f'{command} >/dev/null 2>&1'
    )
    return '\n'.join(kept).rstrip() + '\n\n' + begin + '\n' + line + '\n' + end + '\n'


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


def install(project, python, entry, *, start=True) -> str:
    entry = Path(entry).resolve()
    if not entry.is_file() or not (project.private / 'lark_app.json').is_file():
        raise ValueError('console entry or private lark_app.json is missing')
    current = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if current.returncode not in (0, 1):
        raise RuntimeError('crontab_read_failed')
    revised = cron_text(current.stdout, python, entry, project)
    subprocess.run(['crontab', '-'], input=revised, text=True, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    if project.name == 'yslzm':
        descriptor = os.open(
            project.private / 'run_scheduled.py',
            os.O_WRONLY | os.O_TRUNC | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, 'w') as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(legacy_wrapper(entry, project))
    if start:
        subprocess.Popen(
            serve_command(python, entry, project),
            cwd=entry.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TZ=project.schedule.tz_name),
        )
    return revised
