"""Registry of automated projects."""

from __future__ import annotations

from importlib import import_module

PROJECTS = {'yslzm': 'autocontrol.projects.yslzm:YslzmProject'}


def load(name: str, **options):
    try:
        target = PROJECTS[name]
    except KeyError:
        raise ValueError(
            f'unknown project {name!r}; known: {", ".join(sorted(PROJECTS))}'
        ) from None
    module, _, attr = target.partition(':')
    return getattr(import_module(module), attr)(**options)
