import json
import runpy
import stat
import sys

import pytest

from autocontrol import registry


def entry(name='demo', **fields):
    base = {'name': name, 'manifest': '/work/demo/autocontrol.json', 'private': '/cfg/demo'}
    base.update(fields)
    return base


def test_the_project_list_round_trips_privately(tmp_path):
    path = tmp_path / 'console' / 'projects.json'
    assert registry.load(path) == []
    registry.upsert(path, entry())
    registry.upsert(path, entry('other', manifest=None, project='yslzm'))
    registry.upsert(path, entry(workdir='/work/demo'))  # the same name is replaced in place
    items = registry.load(path)
    assert [item['name'] for item in items] == ['demo', 'other']
    assert items[0]['workdir'] == '/work/demo' and items[0]['enabled'] is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert registry.remove(path, 'demo') and not registry.remove(path, 'demo')
    assert [item['name'] for item in registry.load(path)] == ['other']


@pytest.mark.parametrize(
    'bad',
    [
        entry(private=None),
        entry(project='yslzm'),
        entry(manifest='relative/autocontrol.json'),
        entry(name='Bad Name'),
        entry(enabled='yes'),
    ],
)
def test_invalid_entries_are_refused(tmp_path, bad):
    with pytest.raises(registry.RegistryError):
        registry.upsert(tmp_path / 'console' / 'projects.json', bad)


def test_a_broken_or_duplicated_list_is_an_error(tmp_path):
    path = tmp_path / 'projects.json'
    item = json.dumps(entry())
    path.write_text('{"version": 1, "projects": [%s, %s]}' % (item, item))
    with pytest.raises(registry.RegistryError):
        registry.load(path)
    path.write_text('not json')
    with pytest.raises(registry.RegistryError):
        registry.load(path)


def test_the_entry_for_a_loaded_project(project):
    item = registry.entry_for(project)
    assert item['project'] == 'yslzm' and 'manifest' not in item
    assert item['private'] == str(project.private.resolve())
    assert item['workdir'] == str(project.workdir.resolve())


def test_register_needs_admission_but_no_card_config(tmp_path, capsys):
    from autocontrol.admission import run_check
    from autocontrol.cli import main

    from .helpers import demo_copy

    manifest = demo_copy(tmp_path)
    path = tmp_path / 'console' / 'projects.json'
    args = ['register', '--manifest', str(manifest), '--registry', str(path)]
    assert main(args) == 3  # not admitted yet
    run_check(manifest)
    # Without a bot config the project still runs; it only shows on the web board.
    assert main(args) == 0
    assert '只在看板上显示' in capsys.readouterr().out
    [item] = registry.load(path)
    assert item['manifest'] == str(manifest.resolve())
    assert item['private'] == str((tmp_path / 'private').resolve())
    assert main(['unregister', item['name'], '--registry', str(path)]) == 0
    assert registry.load(path) == []


def test_legacy_wrapper_routes_old_callers_into_the_console(project, monkeypatch):
    source = compile(
        registry.legacy_wrapper('/console/run.py', project), 'run_scheduled.py', 'exec'
    )
    captured = {}
    monkeypatch.setattr(
        runpy, 'run_path', lambda path, run_name: captured.update(argv=list(sys.argv))
    )
    cases = (
        (
            ['run_scheduled.py', 'cardbook', '--not-before-game-day', '2026-11-17'],
            ['--phase', 'cardbook'],
        ),
        (
            ['run_scheduled.py', 'auto', '--not-before-game-day', '2026-11-17', '--full-checklist'],
            [],
        ),
    )
    for argv, expected in cases:
        monkeypatch.setattr(sys, 'argv', argv)
        exec(source, {'__name__': '__main__'})
        assert captured['argv'][:4] == ['/console/run.py', 'once', '--project', 'yslzm']
        assert captured['argv'][4:8] == [
            '--workdir',
            str(project.workdir),
            '--private',
            str(project.private),
        ]
        assert captured['argv'][8:] == expected
