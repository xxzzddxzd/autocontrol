import runpy
import sys

from autocontrol.install import cron_text, legacy_wrapper


def test_install_replaces_only_its_own_and_legacy_entries_and_is_idempotent(project):
    original = (
        'SHELL=/bin/zsh\n0 1 * * * other-project\n'
        '0 6 * * * old # yslzm-daily-auto\n'
        '15 1,9 * * * old # yslzm-cardbook-resume\n'
        '# BEGIN YSLZM LOCAL DAILY BOARD\n*/5 * * * * caffeinate local_daily.py\n# END YSLZM LOCAL DAILY BOARD\n'
    )
    revised = cron_text(original, '/python', '/console/run.py', project)
    assert '0 1 * * * other-project' in revised and 'SHELL=/bin/zsh' in revised
    assert 'yslzm-daily-auto' not in revised and 'local_daily.py' not in revised
    assert revised.count('*/5 * * * *') == 1
    assert '/console/run.py serve --project yslzm' in revised
    assert 'TZ=Asia/Shanghai' in revised and '.log' not in revised
    assert cron_text(revised, '/python', '/console/run.py', project) == revised


def test_legacy_wrapper_routes_old_callers_into_the_console(project, monkeypatch):
    source = compile(legacy_wrapper('/console/run.py', project), 'run_scheduled.py', 'exec')
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
