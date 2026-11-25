from datetime import datetime, timedelta
import json
import signal
import stat
from types import SimpleNamespace

from autocontrol import registry
from autocontrol.console import (
    CODE_SETTLE,
    ELSEWHERE_RETRY,
    FIRST_RETRY,
    HEALTHY,
    INVALID_RETRY,
    QUIT_GRACE,
    RELOAD,
    Console,
    restart_blocker,
)
from autocontrol.schedule import BEIJING


class FakeProcess:
    def __init__(self, command, pid, **options):
        self.command, self.pid, self.options = command, pid, options
        self.code = None
        self.idle = False  # an idle scheduler exits at once when asked to stop
        self.signals = []

    def poll(self):
        return self.code

    def send_signal(self, number):
        self.signals.append(number)
        if self.idle:
            self.code = 0


def item(name, **fields):
    return {'name': name, 'manifest': f'/work/{name}/autocontrol.json',
            'private': f'/cfg/{name}', **fields}  # fmt: skip


class Harness:
    def __init__(self, tmp_path, *entries):
        self.path = tmp_path / 'console' / 'projects.json'
        registry.save(self.path, list(entries))
        self.now = 0.0
        self.busy = set()  # schedulers running outside the console
        self.spawned = []

        def spawn(command, **options):
            self.spawned.append(FakeProcess(command, 1000 + len(self.spawned), **options))
            return self.spawned[-1]

        def loader(entry):
            return SimpleNamespace(name=entry['name'], coordinator_lock=f"/locks/{entry['name']}",
                                   schedule=SimpleNamespace(tz_name='Asia/Shanghai'))  # fmt: skip

        def sleep(seconds):
            self.now += seconds

        self.console = Console(self.path, entry='/console/run.py', python='/py', spawn=spawn,
                               lock_free=lambda lock: lock not in self.busy, clock=lambda: self.now,
                               sleep=sleep, loader=loader, keep_awake=False,
                               check_code=lambda code: True)  # fmt: skip

    def step(self, seconds=0.0, *, reload=False):
        self.now += seconds
        self.console.reload(self.now, force=reload)
        self.console.refresh(self.now)
        self.console.tend(self.now)

    def running(self):
        return {name: slot.process for name, slot in self.console.slots.items() if slot.alive()}


def test_every_enabled_project_gets_one_scheduler_that_reads_first(tmp_path):
    harness = Harness(tmp_path, item('alpha'), item('beta', enabled=False))
    harness.step()
    assert list(harness.running()) == ['alpha']
    process = harness.running()['alpha']
    assert process.command[:4] == ['/py', '-B', '/console/run.py', 'serve']
    assert process.command[-1] == '--observe-first' and '--manifest' in process.command
    assert process.command[4:6] == ['--reason', 'boot']
    assert process.options['env']['TZ'] == 'Asia/Shanghai'
    assert process.options['start_new_session'] is True
    assert harness.console.slots['beta'].state == 'disabled'
    harness.step(60)
    assert len(harness.spawned) == 1  # a running scheduler is left alone


def test_a_scheduler_that_exits_is_restarted_after_a_growing_pause(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    harness.step()
    harness.spawned[-1].code = 1
    harness.step(1)
    assert harness.running() == {} and harness.console.slots['alpha'].state == 'restarting'
    harness.step(FIRST_RETRY - 2)
    assert len(harness.spawned) == 1
    harness.step(2)
    assert len(harness.spawned) == 2  # restarted after 30 s
    assert harness.spawned[-1].command[4:6] == ['--reason', 'crash']
    assert harness.spawned[-1].command[-1] == '--observe-first'
    harness.spawned[-1].code = 1
    harness.step(1)
    harness.step(FIRST_RETRY)
    assert len(harness.spawned) == 2  # the second pause is twice as long
    harness.step(FIRST_RETRY)
    assert len(harness.spawned) == 3
    harness.step(HEALTHY)  # a long healthy run ...
    harness.spawned[-1].code = 9
    harness.step(1)
    harness.step(FIRST_RETRY)
    assert len(harness.spawned) == 4  # ... starts the back-off afresh
    assert harness.console.slots['alpha'].restarts == 3


def test_a_clean_stop_from_outside_is_restarted_without_counting_a_failure(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    harness.step()
    harness.spawned[-1].code = 0  # someone sent SIGTERM to restart it
    harness.step(1)
    slot = harness.console.slots['alpha']
    assert slot.restarts == 0 and slot.state == 'restarting'
    harness.step(FIRST_RETRY)
    assert len(harness.spawned) == 2
    assert harness.spawned[-1].command[4:6] == ['--reason', 'restart']


def test_a_scheduler_running_outside_the_console_is_taken_over_when_it_ends(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    harness.busy.add('/locks/alpha')
    harness.step()
    assert harness.spawned == [] and harness.console.slots['alpha'].state == 'elsewhere'
    harness.busy.clear()
    harness.step(ELSEWHERE_RETRY - 1)
    assert harness.spawned == []
    harness.step(1)
    assert len(harness.spawned) == 1


def test_list_changes_stop_schedulers_after_their_round(tmp_path):
    harness = Harness(tmp_path, item('alpha'), item('beta'), item('gamma'))
    harness.step()
    alpha, beta, gamma = harness.spawned
    registry.save(harness.path, [item('alpha', workdir='/work/alpha'), item('beta', enabled=False)])
    harness.step(1, reload=True)
    assert alpha.signals == beta.signals == gamma.signals == [signal.SIGTERM]
    assert len(harness.spawned) == 3  # nothing starts while the old ones finish their round
    for process in (alpha, beta, gamma):
        process.code = 0
    harness.step(1)
    assert len(harness.spawned) == 4 and '--workdir' in harness.spawned[-1].command
    assert set(harness.console.slots) == {'alpha', 'beta'}
    assert harness.console.slots['beta'].state == 'disabled'
    assert harness.console.slots['alpha'].restarts == 0  # a requested stop is no failure


def test_a_project_that_cannot_load_waits_and_is_retried(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    calls = []

    def broken(entry):
        calls.append(entry['name'])
        raise ValueError('bad manifest')

    harness.console.loader = broken
    harness.step()
    slot = harness.console.slots['alpha']
    assert slot.state == 'invalid' and 'bad manifest' in slot.problem and harness.spawned == []
    harness.step(INVALID_RETRY)
    assert calls == ['alpha', 'alpha']


def test_shutdown_never_cuts_a_round_short(tmp_path):
    harness = Harness(tmp_path, item('alpha'), item('beta'))
    harness.step()
    alpha, beta = harness.spawned
    alpha.idle = True
    before = harness.now
    harness.console.shutdown()
    assert alpha.signals == beta.signals == [signal.SIGTERM]
    assert harness.console.slots['alpha'].state == 'stopped'
    assert harness.console.slots['beta'].state == 'stopping'  # finishes its round, then exits
    assert harness.now - before <= QUIT_GRACE + 1


def test_run_keeps_a_private_status_and_lock(tmp_path):
    harness = Harness(tmp_path, item('alpha'))

    class OneLoop:
        calls = 0

        def is_set(self):
            OneLoop.calls += 1
            return OneLoop.calls > 1

        def wait(self, seconds):
            pass

    assert harness.console.run(OneLoop()) == 0
    status = json.loads(harness.console.status_path.read_text())
    assert status['projects']['alpha']['state'] == 'stopping'
    assert status['projects']['alpha']['enabled'] is True
    assert stat.S_IMODE(harness.console.status_path.stat().st_mode) == 0o600
    assert harness.console.lock_path.exists()


def test_an_admitted_manifest_change_loads_between_rounds(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    console = harness.console
    digests, passed, blocker = {'alpha': 'one'}, {'alpha': 'one'}, ['正在跑一轮']
    console.digest = lambda entry: digests[entry['name']]
    console.passed = lambda entry: passed[entry['name']]
    console.quiet = lambda slot: blocker[0]
    harness.step()
    first, slot = harness.spawned[-1], console.slots['alpha']
    assert slot.digest == 'one'
    digests['alpha'] = 'two'  # edited but not admitted: the old manifest keeps running
    harness.step(RELOAD)
    assert first.signals == [] and 'run.py check' in slot.pending
    passed['alpha'] = 'two'  # admitted, but a round is running
    harness.step(RELOAD)
    assert first.signals == [] and '正在跑一轮' in slot.pending
    blocker[0] = None  # between rounds: stop it, then start it again at once
    harness.step(RELOAD)
    assert first.signals == [signal.SIGTERM] and slot.state == 'reloading'
    first.code = 0
    harness.step(1)
    assert len(harness.spawned) == 2 and harness.spawned[-1].command[-1] == '--observe-first'
    assert harness.spawned[-1].command[4:6] == ['--reason', 'manifest']
    assert (slot.digest, slot.reloads, slot.restarts, slot.pending) == ('two', 1, 0, None)
    harness.step(RELOAD)
    assert len(harness.spawned) == 2  # nothing more to load
    status = console.status()['projects']['alpha']
    assert status['reloads'] == 1 and status['pending'] is None


def test_a_reload_that_ends_in_an_error_backs_off_like_any_failure(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    console = harness.console
    digests = {'alpha': 'one'}
    console.digest = lambda entry: digests[entry['name']]
    console.passed = lambda entry: 'two'
    console.quiet = lambda slot: None
    harness.step()
    digests['alpha'] = 'two'
    harness.step(RELOAD)
    harness.spawned[-1].code = 1
    harness.step(1)
    slot = console.slots['alpha']
    assert slot.restarts == 1 and slot.reloads == 0 and len(harness.spawned) == 1
    harness.step(FIRST_RETRY)
    assert len(harness.spawned) == 2


def test_a_new_manifest_never_loads_during_a_round_or_right_around_a_wake():
    now = datetime(2026, 11, 19, 11, 0, tzinfo=BEIJING)
    assert restart_blocker(now, running=True) == '正在跑一轮'
    assert '11:09' in restart_blocker(now, running=False, due=(now + timedelta(minutes=9),))
    # A wake that has just passed may still be starting its round.
    assert '10:58' in restart_blocker(now, running=False, due=(now - timedelta(minutes=2),))
    far = (now + timedelta(minutes=11), now - timedelta(minutes=30), None)
    assert restart_blocker(now, running=False, due=far) is None


def test_the_console_reads_the_digest_that_admission_binds(tmp_path):
    from autocontrol.admission import passed_digest, save_record
    from autocontrol.projects.manifest import ManifestProject, manifest_digest

    from .helpers import demo_copy

    project = ManifestProject(demo_copy(tmp_path))
    assert manifest_digest(project.manifest_path) == project.digest
    assert manifest_digest(tmp_path / 'missing.json') is None
    assert passed_digest(project.private, project.name) is None
    save_record(project, {'digest': project.digest, 'passed': True})
    assert passed_digest(project.private, project.name) == project.digest
    save_record(project, {'digest': project.digest, 'passed': False})
    assert passed_digest(project.private, project.name) is None


def test_new_console_code_restarts_each_scheduler_between_rounds(tmp_path):
    adapter = {'name': 'beta', 'project': 'beta', 'private': '/cfg/beta'}
    harness = Harness(tmp_path, item('alpha'), adapter)
    console = harness.console
    codes, blocker = ['v1'], ['正在跑一轮']
    console.code = lambda: codes[0]
    console.digest = lambda entry: 'one' if entry.get('manifest') else None
    console.passed = lambda entry: 'one'
    console.quiet = lambda slot: blocker[0]
    harness.step()
    alpha, beta = harness.spawned
    assert [console.slots[name].code for name in ('alpha', 'beta')] == ['v1', 'v1']
    codes[0] = 'v2'  # a deploy: the files may still be changing
    harness.step(RELOAD)
    assert alpha.signals == beta.signals == [] and console.slots['alpha'].pending is None
    harness.step(CODE_SETTLE)  # settled, but a round is running
    assert alpha.signals == [] and '总控代码已更新' in console.slots['alpha'].pending
    assert '正在跑一轮' in console.slots['beta'].pending  # a built-in project too
    blocker[0] = None
    harness.step(RELOAD)
    assert alpha.signals == beta.signals == [signal.SIGTERM]
    alpha.code = beta.code = 0
    harness.step(1)
    assert len(harness.spawned) == 4
    for name in ('alpha', 'beta'):
        slot = console.slots[name]
        assert (slot.code, slot.reloads, slot.restarts, slot.pending) == ('v2', 1, 0, None)
    # Only the code changed: the new schedulers take up the saved card without a login.
    for process in harness.spawned[2:]:
        assert process.command[4:6] == ['--reason', 'code'] and process.command[-1] == '--resume'


def test_an_unadmitted_manifest_holds_back_new_code(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    console = harness.console
    codes, digests = ['v1'], {'alpha': 'one'}
    console.code = lambda: codes[0]
    console.digest = lambda entry: digests[entry['name']]
    console.passed = lambda entry: 'one'
    console.quiet = lambda slot: None
    harness.step()
    digests['alpha'], codes[0] = 'two', 'v2'  # a restart now would be refused
    harness.step(RELOAD)
    harness.step(CODE_SETTLE)
    assert harness.spawned[0].signals == [] and 'run.py check' in console.slots['alpha'].pending


def test_the_console_runs_its_own_new_code_and_hands_over_its_schedulers(tmp_path):
    import os
    import sys

    harness = Harness(tmp_path, item('alpha'))
    console = harness.console
    codes, calls = ['v1'], []
    console.code = lambda: codes[0]
    console.quiet = lambda slot: '正在跑一轮'

    def execv(path, argv):  # a stand-in that returns, as a failed exec would
        calls.append((path, argv, json.loads(console.handover_path.read_text())))

    console.execv = execv
    console._own_code = 'v1'  # as run() records it
    harness.step()
    codes[0] = 'v2'
    harness.step(RELOAD)
    assert console.upgrade(harness.now) is False  # the files may still be changing
    harness.step(CODE_SETTLE)
    slot = console.slots['alpha']
    slot.reloading = True
    assert console.upgrade(harness.now) is False  # never in the middle of a stop
    slot.reloading = False
    assert console.upgrade(harness.now) is True
    path, argv, handover = calls[0]
    assert path == sys.executable and argv[0] == sys.executable
    assert argv[1:] == ['-B', '/console/run.py', 'console', '--registry', str(harness.path)]
    assert handover['pid'] == os.getpid()
    assert handover['slots']['alpha']['pid'] == 1000
    assert handover['slots']['alpha']['code'] == 'v1'
    assert not console.handover_path.exists()  # the exec failed: nothing left behind
    assert console.upgrade(harness.now + 1) is False  # tried again only much later


def test_the_new_code_takes_back_the_schedulers_this_process_started(tmp_path):
    import os
    import sys
    import time

    child = os.posix_spawn(
        sys.executable, [sys.executable, '-c', 'import time; time.sleep(60)'], dict(os.environ)
    )
    harness = Harness(tmp_path, item('alpha'), item('beta'))
    console = harness.console
    saved = {'started': 0.0, 'started_at': 'then', 'digest': 'one', 'code': 'old',
             'failures': 0, 'restarts': 2, 'reloads': 1, 'last_exit': None}  # fmt: skip
    slots = {'alpha': {**saved, 'pid': child}, 'beta': {**saved, 'pid': 999999}}
    console._write_private(console.handover_path, {'pid': os.getpid() + 1, 'slots': slots})
    console.reload(0.0, force=True)
    console._adopt()  # left by another process: ignored, and removed
    assert console.slots['alpha'].process is None and not console.handover_path.exists()
    console._write_private(console.handover_path, {'pid': os.getpid(), 'slots': slots})
    console._adopt()
    alpha, beta = console.slots['alpha'], console.slots['beta']
    assert alpha.process.pid == child and alpha.state == 'running'
    assert (alpha.code, alpha.digest, alpha.restarts, alpha.reloads) == ('old', 'one', 2, 1)
    assert beta.process is None  # not this process's child
    harness.step()
    assert [process.command[-2] for process in harness.spawned] == ['/cfg/beta']
    alpha.process.send_signal(signal.SIGTERM)
    deadline = time.time() + 10
    while alpha.process.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert alpha.process.poll() == -signal.SIGTERM
    harness.step(1)
    assert alpha.process is None and alpha.state == 'restarting'


def test_new_code_that_does_not_import_is_never_loaded(tmp_path):
    harness = Harness(tmp_path, item('alpha'))
    console = harness.console
    codes, checked = ['v1'], []
    console.code = lambda: codes[0]
    console.quiet = lambda slot: None
    console.check_code = lambda code: checked.append(code) or False
    console._own_code = 'v1'
    harness.step()
    codes[0] = 'broken'
    harness.step(RELOAD)
    harness.step(CODE_SETTLE)
    assert harness.spawned[0].signals == [] and '导入失败' in console.slots['alpha'].pending
    assert console.upgrade(harness.now) is False and checked and set(checked) == {'broken'}


def test_the_import_check_runs_once_per_code(tmp_path, monkeypatch):
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    console = Console(tmp_path / 'a' / 'projects.json', entry=root / 'run.py',
                      python=sys.executable, keep_awake=False)  # fmt: skip
    calls, real = [], subprocess.run

    def counting(*args, **options):
        calls.append(args[0])
        return real(*args, **options)

    monkeypatch.setattr(subprocess, 'run', counting)
    assert console._code_works('a') is True and console._code_works('a') is True
    assert len(calls) == 1
    broken = Console(tmp_path / 'b' / 'projects.json', entry=root / 'run.py',
                     python='/no/such/python', keep_awake=False)  # fmt: skip
    assert broken._code_works('a') is False
