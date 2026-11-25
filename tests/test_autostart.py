import plistlib
from types import SimpleNamespace

from autocontrol import autostart


def test_the_agent_runs_only_the_console_and_restarts_it_only_on_failure(tmp_path):
    entry = tmp_path / 'run.py'
    entry.write_text('')
    agent = autostart.agent('/py', entry, log=tmp_path / 'console.log')
    assert agent['Label'] == autostart.LABEL
    assert agent['ProgramArguments'] == ['/py', '-B', str(entry.resolve()), 'console']
    assert agent['RunAtLoad'] is True and agent['KeepAlive'] == {'SuccessfulExit': False}
    assert '/usr/bin' in agent['EnvironmentVariables']['PATH'].split(':')
    assert agent['ExitTimeOut'] > 30  # room for the console's own graceful stop


def test_enable_writes_and_loads_the_agent_and_disable_removes_it(tmp_path, monkeypatch):
    target = tmp_path / 'LaunchAgents' / f'{autostart.LABEL}.plist'
    monkeypatch.setattr(autostart, 'plist_path', lambda: target)
    calls = []

    def run(command, **options):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='')

    log = tmp_path / 'console' / 'logs' / 'console.log'
    assert autostart.enable('/py', tmp_path / 'run.py', log=log, run=run) == target
    assert plistlib.loads(target.read_bytes())['ProgramArguments'][-1] == 'console'
    assert log.parent.is_dir() and (log.parent.stat().st_mode & 0o777) == 0o700
    assert [command[1] for command in calls] == ['bootout', 'bootstrap']
    assert calls[1][2] == autostart.service().rsplit('/', 1)[0]
    assert autostart.disable(run=run) and not target.exists()
    assert not autostart.disable(run=run)


def test_state_reads_launchctl(monkeypatch):
    def loaded(command, **options):
        return SimpleNamespace(
            returncode=0, stdout='gui/501/x = {\n\tstate = running\n\tpid = 7\n}'
        )

    assert autostart.state(run=loaded) == 'running'
    assert autostart.state(run=lambda command, **options: SimpleNamespace(returncode=113)) is None
