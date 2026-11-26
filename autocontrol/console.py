"""The console process: one resident process that runs every registered project.

Each project's own scheduler (``serve``) still decides when to run what: fixed
times, server wakes, catch-up. This process only keeps those schedulers
running. It starts one ``serve`` per enabled project with ``--observe-first``
so a restart never replays a round (after new console code alone it uses
``--resume``: nothing changed in the game, so it logs in only at its next
wake); restarts one that exits after a growing pause; and stops one after its
current round when its
project is disabled, removed or changed in the project list. A scheduler reads
its manifest once, at start: when the file changes and passes admission again,
the console restarts that scheduler between rounds so the new manifest loads
without anyone restarting it. New console code loads the same way: once the
files have settled, each scheduler restarts between rounds, and this process
runs its own new code in place (same pid) and takes its schedulers back. A
project whose scheduler already runs outside the console (a ``serve`` started
by hand) is left alone and taken over once that process ends.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from . import locks, registry

POLL = 5.0  # seconds between checks of the schedulers
RELOAD = 30.0  # seconds between checks of the project list
FIRST_RETRY = 30.0
MAX_RETRY = 600.0
HEALTHY = 600.0  # a run at least this long starts the back-off afresh
ELSEWHERE_RETRY = 60.0
INVALID_RETRY = 600.0
QUIT_GRACE = 30.0  # how long shutdown waits for idle schedulers to exit
RELOAD_MARGIN = 600.0  # a new manifest loads only when no wake is this close
CODE_SETTLE = 60.0  # new console code counts once its files stayed the same this long
REPLACE_RETRY = 600.0  # after a failed self-replacement, try again this much later
LOG_LIMIT = 1 << 20
HELPER_STOP_GRACE = 5.0  # how long a helper (web board, tunnel) gets to exit before SIGKILL
HELPER_ELSEWHERE = 75  # a helper's exit code when another copy of it already runs
# How a scheduler start is described in the console's log (cli.START_REASONS).
HELPER_NAMES = {'dashboard': '看板', 'tunnel': '隧道'}

REASON_TEXT = {
    'boot': '总控启动',
    'code': '总控代码更新',
    'manifest': '新清单通过准入',
    'settings': '登记表设置变更',
    'restart': '被停止后重启',
    'crash': '异常退出后重启',
    'manual': '手动启动',
}


def retry_after(failures: int) -> float:
    return min(MAX_RETRY, FIRST_RETRY * 2 ** min(16, max(0, failures - 1)))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def restart_blocker(now, *, running, due=(), margin=RELOAD_MARGIN) -> str | None:
    """Why a scheduler should not be restarted now, or None.

    ``running``: a round holds the project. ``due``: wake times, past or
    future. A restart right before a wake skips that round (the new scheduler
    reads first and never replays one), and a wake that has just passed may
    still be starting its round.
    """
    if running:
        return '正在跑一轮'
    for when in due:
        if when is not None and abs((when - now).total_seconds()) < margin:
            return f'{when:%H:%M} 的一轮离现在不到 {int(margin // 60)} 分钟'
    return None


def code_digest(root=None) -> str | None:
    """Fingerprint of the console code: every ``.py`` file of the package, and run.py."""
    base = Path(root) if root else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    try:
        files = sorted(path for path in base.rglob('*.py') if '__pycache__' not in path.parts)
        entry = base.parent / 'run.py'
        for path in [*files, *([entry] if entry.is_file() else [])]:
            digest.update(str(path.relative_to(base.parent)).encode() + b'\0')
            digest.update(path.read_bytes() + b'\0')
    except OSError:
        return None
    return digest.hexdigest()


class Adopted:
    """A scheduler this process started before it replaced its own code.

    It is still this process's child, so its exit status can be collected.
    """

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None if type(pid) is int and pid > 0 else -1

    def poll(self):
        if self.returncode is None:
            try:
                done, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self.returncode = -1  # not this process's child: nothing to collect
            else:
                if done:
                    self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def send_signal(self, number) -> None:
        if self.poll() is None:
            os.kill(self.pid, number)


def _manifest_digest(entry):
    if not entry.get('manifest'):
        return None
    from .projects.manifest import manifest_digest

    return manifest_digest(entry['manifest'])


def _passed_digest(entry):
    from .admission import passed_digest

    return passed_digest(entry['private'], entry['name'])


def load_project(entry):
    """The project a registry entry names, loaded as the command line would."""
    options = {
        'workdir': Path(entry['workdir']) if entry.get('workdir') else None,
        'private': Path(entry['private']),
        'python': entry.get('python'),
    }
    if entry.get('manifest'):
        from .projects.manifest import ManifestProject

        return ManifestProject(Path(entry['manifest']), **options)
    from . import projects

    return projects.load(entry['project'], **options)


class Slot:
    """One registered project and the scheduler process the console runs for it."""

    def __init__(self, entry):
        self.entry = entry
        self.project = None
        self.problem = None
        self.process = None
        self.started = None  # monotonic start of the current process
        self.started_at = None
        self.failures = 0
        self.restarts = 0
        self.next_start = 0.0
        self.state = 'pending'
        self.stopping = False
        self.removed = False
        self.last_exit = None
        self.digest = None  # the manifest the running scheduler loaded
        self.code = None  # the console code it loaded
        self.reloading = False  # stopped to load a changed manifest
        self.reload_kind = None  # what it was stopped to load: 'manifest' or 'code'
        self.reloads = 0
        self.pending = None  # why a changed manifest is not loaded yet
        self.start_reason = 'boot'  # why its next start happens (see cli.START_REASONS)
        self.said = None  # the last pending note written to the log

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


class Helper:
    """A process kept running next to the schedulers: the web board and its tunnel.

    Unlike a scheduler it holds no round, so it is restarted at any time: when
    it exits, when its command changes, and around a console code change (the
    console stops it before running its new code, which starts it again).
    """

    def __init__(self, name, command):
        self.name = name
        self.command = command
        self.process = None
        self.started = None
        self.started_at = None
        self.failures = 0
        self.restarts = 0
        self.next_start = 0.0
        self.state = 'pending'
        self.stopping = False
        self.last_exit = None
        self.problem = None

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


class Console:
    def __init__(self, registry_path=None, *, entry, python=None, spawn=subprocess.Popen,
                 lock_free=locks.is_free, clock=time.monotonic, sleep=time.sleep,
                 loader=load_project, keep_awake=True, digest=_manifest_digest,
                 passed=_passed_digest, quiet=None, code=code_digest, execv=os.execv,
                 check_code=None):  # fmt: skip
        self.registry_path = Path(registry_path) if registry_path else registry.default_path()
        self.dir = self.registry_path.parent
        self.entry = Path(entry)
        self.python = python or sys.executable
        self.spawn = spawn
        self.lock_free = lock_free
        self.clock = clock
        self.sleep = sleep
        self.loader = loader
        self.keep_awake = keep_awake
        self.digest = digest
        self.passed = passed
        self.quiet = quiet or self._quiet
        self.code = code
        self.execv = execv
        self.check_code = check_code or self._code_works
        self._code_checked = None  # (code, imports cleanly)
        self._own_code = None  # the code this process runs
        self._code_seen = None  # the code on disk, and since when it is unchanged
        self._code_since = 0.0
        self._next_replace = 0.0
        self._awake = None
        self.slots: dict[str, Slot] = {}
        self.helpers: dict[str, Helper] = {}
        self.helper_error = None
        self.registry_error = None
        self._mtime = None
        self._next_reload = None
        self._next_refresh = None
        self._started_at = _now()

    @property
    def lock_path(self) -> Path:
        return self.dir / 'console.lock'

    @property
    def status_path(self) -> Path:
        return self.dir / 'status.json'

    @property
    def handover_path(self) -> Path:
        return self.dir / 'handover.json'

    # -- the loop --------------------------------------------------------------

    def run(self, stop) -> int:
        """Keep every enabled project's scheduler running until ``stop`` is set."""
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        self.dir.mkdir(mode=0o700, exist_ok=True)
        fd = locks.acquire(self.lock_path)  # AlreadyRunning: another console runs
        self._awake = self._stay_awake()
        self._own_code = self.code()
        self._say(f'总控运行，pid {os.getpid()}')
        self.reload(self.clock(), force=True)
        self._adopt()
        try:
            while not stop.is_set():
                now = self.clock()
                self.reload(now)
                self.refresh(now)
                self.tend(now)
                self.tend_helpers(now)
                self.write_status()
                self.upgrade(now)
                stop.wait(POLL)
        finally:
            self.shutdown()
            self.write_status()
            if self._awake is not None:
                self._awake.terminate()
            os.close(fd)
        return 0

    def reload(self, now=None, *, force=False) -> None:
        """Pick up project list changes; a broken list keeps what already runs."""
        now = self.clock() if now is None else now
        if not force and self._next_reload is not None and now < self._next_reload:
            return
        self._next_reload = now + RELOAD
        try:
            mtime = self.registry_path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if not force and self._mtime is not None and mtime == self._mtime:
            return
        self._mtime = mtime if mtime is not None else 0
        try:
            entries = registry.load(self.registry_path)
        except registry.RegistryError as error:
            self.registry_error = str(error)
            return
        self.registry_error = None
        wanted = {entry['name']: entry for entry in entries}
        for name, entry in wanted.items():
            slot = self.slots.get(name)
            if slot is None:
                self.slots[name] = Slot(entry)
                continue
            slot.removed = False
            changed = {
                key
                for key in set(slot.entry) | set(entry)
                if key != 'enabled' and slot.entry.get(key) != entry.get(key)
            }
            slot.entry = entry
            if changed:
                # New settings: reload them and restart after the current round.
                slot.project = None
                slot.next_start = now
                slot.start_reason = 'settings'
                self._say(f"{name}：登记表设置变更，这一轮结束后重启")
                self._stop(slot)
            elif not entry['enabled']:
                self._stop(slot)
        for name, slot in self.slots.items():
            if name not in wanted:
                slot.removed = True
                self._stop(slot)

    def refresh(self, now=None) -> None:
        """Load what changed under the running schedulers, between rounds.

        A changed manifest loads once it passes admission; an unadmitted change
        keeps the old manifest running (a restart would be refused until
        ``check`` passes). New console code loads once its files have settled.
        Either way the scheduler is stopped while no round runs or is due, and
        starts again at once.
        """
        now = self.clock() if now is None else now
        if self._next_refresh is not None and now < self._next_refresh:
            return
        self._next_refresh = now + RELOAD
        code = self.code()
        if code != self._code_seen:
            self._code_seen, self._code_since = code, now
        settled = code is not None and now - self._code_since >= CODE_SETTLE
        broken = False  # new code that does not even import is never loaded
        if settled and any(slot.code not in (None, code) for slot in self.slots.values()):
            broken = not self.check_code(code)
        for slot in self.slots.values():
            if not slot.alive() or slot.stopping or slot.reloading:
                continue
            load = None
            if slot.entry.get('manifest') and slot.digest is not None:
                digest = self.digest(slot.entry)
                if digest is None:
                    continue  # unreadable for a moment (being saved); look again later
                if digest != slot.digest:
                    if self.passed(slot.entry) != digest:
                        slot.pending = '清单已改，运行 run.py check 通过准入后自动加载'
                        continue
                    load = '新清单已通过准入'
            if load is None and settled and slot.code is not None and slot.code != code:
                if broken:
                    slot.pending = '总控新代码导入失败，暂不加载'
                    continue
                load = '总控代码已更新'
            if load is None:
                slot.pending = None
                continue
            why = self.quiet(slot)
            if why:
                slot.pending = f'{load}，等两轮之间加载（{why}）'
                if slot.said != slot.pending:
                    slot.said = slot.pending
                    self._say(f"{slot.entry['name']}：{slot.pending}")
                continue
            slot.pending = slot.said = None
            slot.reloading = True
            slot.reload_kind = 'manifest' if load == '新清单已通过准入' else 'code'
            slot.state = 'reloading'
            self._say(f"{slot.entry['name']}：{load}，现在两轮之间，停下重启")
            self._signal(slot)

    def tend(self, now=None) -> None:
        """Start what should run, notice exits and schedule restarts."""
        now = self.clock() if now is None else now
        for name in list(self.slots):
            slot = self.slots[name]
            if slot.process is not None:
                code = slot.process.poll()
                if code is None:
                    continue
                self._exited(slot, code, now)
            if slot.removed:
                del self.slots[name]
                continue
            if not slot.entry['enabled']:
                slot.state = 'disabled'
                continue
            if now < slot.next_start:
                continue
            if slot.project is None and not self._prepare(slot, now):
                continue
            if not self.lock_free(slot.project.coordinator_lock):
                # A scheduler started outside the console still runs this project.
                slot.state = 'elsewhere'
                slot.next_start = now + ELSEWHERE_RETRY
                continue
            self._start(slot, now)

    def shutdown(self) -> None:
        """Ask every scheduler to stop after its current round.

        Idle ones exit at once; one in the middle of a round finishes it and
        then exits by itself, so shutdown never cuts a round short.
        """
        self.stop_helpers()
        for slot in self.slots.values():
            if slot.alive():
                self._signal(slot)
                slot.state = 'stopping'
        deadline = self.clock() + QUIT_GRACE
        while self.clock() < deadline and any(slot.alive() for slot in self.slots.values()):
            self.sleep(0.2)
        for slot in self.slots.values():
            if slot.process is not None and not slot.alive():
                slot.process = None
                slot.state = 'stopped'

    # -- helper processes (web board, tunnel) ----------------------------------------

    def helper_commands(self) -> dict[str, list[str]]:
        """The helpers ``dashboard.json`` asks for; none when it is missing or off."""
        from .dashboard import config as board

        try:
            settings = board.load(board.path_for(self.dir))
        except FileNotFoundError:
            self.helper_error = None
            return {}
        except Exception as error:
            self.helper_error = f'看板设置无效：{str(error)[:120]}'
            return {}
        self.helper_error = None
        if not settings['enabled']:
            return {}
        commands = {
            'dashboard': [self.python, '-B', str(self.entry), 'dashboard',
                          '--registry', str(self.registry_path)],
        }  # fmt: skip
        tunnel = settings.get('tunnel')
        if tunnel:
            binary = (
                tunnel.get('binary')
                or shutil.which('cloudflared')
                or '/opt/homebrew/bin/cloudflared'
            )
            config = str(Path(tunnel['config']).expanduser())
            commands['tunnel'] = [binary, 'tunnel', '--no-autoupdate', '--config', config, 'run']
        return commands

    def tend_helpers(self, now=None) -> None:
        """Start the helpers that should run, restart ones that exited or changed."""
        now = self.clock() if now is None else now
        wanted = self.helper_commands()
        for name in list(self.helpers):
            helper = self.helpers[name]
            if helper.process is not None and helper.process.poll() is not None:
                self._helper_exited(helper, helper.process.poll(), now)
            if name in wanted and helper.command == wanted[name]:
                continue
            if helper.alive():
                self._stop_helper(helper)  # it starts again (or goes) once it has exited
            elif name in wanted:
                helper.command, helper.next_start = wanted[name], now
            else:
                del self.helpers[name]
        for name, command in wanted.items():
            helper = self.helpers.setdefault(name, Helper(name, command))
            if helper.process is None and helper.command == command and now >= helper.next_start:
                self._start_helper(helper, now)

    def stop_helpers(self) -> None:
        """Stop every helper now (SIGTERM, then SIGKILL after a short grace)."""
        for helper in self.helpers.values():
            self._stop_helper(helper)
        deadline = self.clock() + HELPER_STOP_GRACE
        while self.clock() < deadline and any(h.alive() for h in self.helpers.values()):
            self.sleep(0.1)
        for helper in self.helpers.values():
            if helper.alive():
                try:
                    helper.process.send_signal(signal.SIGKILL)
                except OSError:
                    pass
            if helper.process is not None and not helper.alive():
                helper.process = None
                helper.state = 'stopped'
                helper.stopping = False
                helper.next_start = 0.0

    def _start_helper(self, helper, now) -> None:
        log = self._log(helper.name)
        try:
            helper.process = self.spawn(
                helper.command,
                cwd=str(self.entry.parent),
                stdin=subprocess.DEVNULL,
                stdout=log if log is not None else subprocess.DEVNULL,
                stderr=log if log is not None else subprocess.DEVNULL,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
                start_new_session=True,
            )
        except OSError as error:
            helper.process = None
            helper.problem = type(error).__name__
            helper.failures += 1
            helper.next_start = now + retry_after(helper.failures)
            helper.state = 'start_failed'
            return
        finally:
            if log is not None:
                log.close()
        helper.started, helper.started_at = now, _now()
        helper.state, helper.problem, helper.stopping = 'running', None, False
        self._say(f'{HELPER_NAMES.get(helper.name, helper.name)}：启动，pid {helper.process.pid}')

    def _helper_exited(self, helper, code, now) -> None:
        ran = now - (helper.started if helper.started is not None else now)
        helper.process = None
        helper.last_exit = {'code': code, 'at': _now(), 'ran_s': int(ran)}
        label = HELPER_NAMES.get(helper.name, helper.name)
        if helper.stopping:
            helper.stopping, helper.failures = False, 0
            helper.next_start, helper.state = now, 'stopped'
            return
        if code == HELPER_ELSEWHERE:
            helper.next_start, helper.state = now + ELSEWHERE_RETRY, 'elsewhere'
            return
        helper.failures = 1 if ran >= HEALTHY else helper.failures + 1
        helper.restarts += 1
        helper.next_start = now + retry_after(helper.failures)
        helper.state = 'restarting'
        pause = int(retry_after(helper.failures))
        self._say(f'{label}：异常退出（退出码 {code}，运行 {int(ran)} 秒），{pause} 秒后重启')

    @staticmethod
    def _stop_helper(helper) -> None:
        if helper.alive():
            helper.stopping = True
            helper.state = 'stopping'
            try:
                helper.process.send_signal(signal.SIGTERM)
            except OSError:
                pass

    # -- helpers ---------------------------------------------------------------

    def command(self, entry, reason='boot') -> list[str]:
        if entry.get('manifest'):
            target = ['--manifest', entry['manifest']]
        else:
            target = ['--project', entry['project']]
        command = [self.python, '-B', str(self.entry), 'serve', '--reason', reason, *target,
                   '--private', entry['private']]  # fmt: skip
        if entry.get('workdir'):
            command += ['--workdir', entry['workdir']]
        if entry.get('python'):
            command += ['--python', entry['python']]
        # Only new console code changed: the saved card is still true, so the
        # scheduler waits for its next wake instead of logging in to read.
        return command + (['--resume'] if reason == 'code' else ['--observe-first'])

    def _prepare(self, slot, now) -> bool:
        try:
            project = self.loader(slot.entry)
            if project.name != slot.entry['name']:
                raise ValueError('the project list name does not match the project')
        except Exception as error:
            slot.project = None
            slot.problem = f'{type(error).__name__}: {str(error)[:120]}'
            slot.state = 'invalid'
            slot.next_start = now + INVALID_RETRY
            return False
        slot.project = project
        slot.problem = None
        return True

    def _start(self, slot, now) -> None:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TZ=slot.project.schedule.tz_name)
        slot.digest = self.digest(slot.entry)  # what the new scheduler is about to load
        slot.code = self.code()
        log = self._log(slot.entry['name'])
        reason, slot.start_reason = slot.start_reason, 'restart'
        try:
            slot.process = self.spawn(
                self.command(slot.entry, reason),
                cwd=str(self.entry.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log if log is not None else subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except OSError as error:
            slot.process = None
            slot.problem = type(error).__name__
            slot.failures += 1
            slot.next_start = now + retry_after(slot.failures)
            slot.state = 'start_failed'
            return
        finally:
            if log is not None:
                log.close()
        slot.started, slot.started_at = now, _now()
        slot.state = 'running'
        how = '不登录，沿用卡片' if reason == 'code' else '先只读读取'
        self._say(
            f"{slot.entry['name']}：启动调度（{REASON_TEXT.get(reason, reason)}，{how}），pid {slot.process.pid}"
        )

    def _exited(self, slot, code, now) -> None:
        ran = now - (slot.started if slot.started is not None else now)
        slot.process = None
        slot.last_exit = {'code': code, 'at': _now(), 'ran_s': int(ran)}
        name = slot.entry['name']
        if slot.stopping:
            slot.stopping = False
            slot.reloading = False
            slot.failures = 0
            slot.state = 'stopped'
            self._say(f'{name}：调度已停止（退出码 {code}）')
            return
        if slot.reloading:
            slot.reloading = False
            kind, slot.reload_kind = slot.reload_kind, None
            if code == 0:
                # Stopped to load its changed manifest: back at once, not a failure.
                slot.failures = 0
                slot.reloads += 1
                slot.next_start = now
                slot.state = 'restarting'
                slot.start_reason = kind or 'manifest'
                self._renew(slot)
                return
        if code == 0:
            # A clean stop someone asked for (SIGTERM to restart it), or another
            # scheduler already runs the project: restart soon, not a failure.
            slot.failures = 0
            slot.next_start = now + FIRST_RETRY
            slot.state = 'restarting'
            slot.start_reason = 'restart'
            self._say(f'{name}：调度退出（退出码 0），{int(FIRST_RETRY)} 秒后重启')
            return
        slot.failures = 1 if ran >= HEALTHY else slot.failures + 1
        slot.restarts += 1
        slot.next_start = now + retry_after(slot.failures)
        slot.state = 'restarting'
        slot.start_reason = 'crash'
        pause = int(retry_after(slot.failures))
        self._say(f'{name}：调度异常退出（退出码 {code}，运行 {int(ran)} 秒），{pause} 秒后重启')

    def _renew(self, slot) -> None:
        """Refresh the console's own view of a reloaded project (its time zone)."""
        try:
            project = self.loader(slot.entry)
        except Exception:
            return  # keep the old view; the scheduler validates its manifest itself
        if project.name == slot.entry['name']:
            slot.project = project

    def _quiet(self, slot) -> str | None:
        """Why a scheduler cannot be restarted right now; None between rounds."""
        project = slot.project
        try:
            # Who holds the round lock, without touching the lock itself: a
            # round starting at that moment must never find it taken.
            lsof = shutil.which('lsof') or '/usr/sbin/lsof'
            probe = subprocess.run(
                [lsof, '-t', str(project.round_lock)], capture_output=True, text=True, timeout=20
            )
            schedule = project.schedule
            now = schedule.now()
            from .lark.store import BoardStore

            board = BoardStore.latest_snapshot(
                project.board_db, project.catalog.project, schedule.service_day(now)
            )
            board = board if type(board) is dict else {}
            due, _ = schedule.next_wake(now, board.get('dynamic_candidates') or ())
            stored = board.get('next_check_at')
            stored = schedule.local(stored) if stored else None
        except Exception as error:
            return f'无法确认是否空闲：{type(error).__name__}'
        running = bool(probe.stdout.split()) or board.get('running') is True
        return restart_blocker(now, running=running, due=(due, stored))

    def _stop(self, slot) -> None:
        if slot.alive():
            slot.stopping = True
            self._signal(slot)
            slot.state = 'stopping'

    @staticmethod
    def _say(text: str) -> None:
        """One timestamped line in the console's own log (its stderr)."""
        try:
            print(f'{_now()} {text}', file=sys.stderr, flush=True)
        except Exception:
            pass  # a log line never affects the console

    @staticmethod
    def _signal(slot) -> None:
        try:
            slot.process.send_signal(signal.SIGTERM)
        except OSError:
            pass

    def _log(self, name):
        """Each scheduler's stderr (tracebacks) goes to ``logs/<name>.log``."""
        try:
            folder = self.dir / 'logs'
            folder.mkdir(mode=0o700, exist_ok=True)
            path = folder / f'{name}.log'
            if path.exists() and path.stat().st_size > LOG_LIMIT:
                os.replace(path, path.with_suffix('.log.1'))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            return os.fdopen(fd, 'ab')
        except OSError:
            return None

    def _stay_awake(self):
        if not self.keep_awake or not Path('/usr/bin/caffeinate').exists():
            return None
        try:
            return subprocess.Popen(
                ['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return None

    def status(self) -> dict:
        return {
            'pid': os.getpid(),
            'started_at': self._started_at,
            'updated_at': _now(),
            'registry_error': self.registry_error,
            'projects': {
                name: {
                    'state': slot.state,
                    'enabled': slot.entry['enabled'],
                    'pid': slot.process.pid if slot.alive() else None,
                    'started_at': slot.started_at if slot.alive() else None,
                    'restarts': slot.restarts,
                    'last_exit': slot.last_exit,
                    'problem': slot.problem,
                    'pending': slot.pending,
                    'reloads': slot.reloads,
                }
                for name, slot in self.slots.items()
            },
            'helpers': {
                name: {
                    'state': helper.state,
                    'pid': helper.process.pid if helper.alive() else None,
                    'started_at': helper.started_at if helper.alive() else None,
                    'restarts': helper.restarts,
                    'last_exit': helper.last_exit,
                    'problem': helper.problem,
                }
                for name, helper in self.helpers.items()
            },
            'helper_error': self.helper_error,
        }

    def write_status(self) -> None:
        try:
            self._write_private(self.status_path, self.status())
        except OSError:
            pass

    @staticmethod
    def _write_private(path, data) -> None:
        temporary = path.with_name(f'.{path.name}.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write('\n')
        os.replace(temporary, path)

    # -- running new console code -----------------------------------------------

    def upgrade(self, now=None) -> bool:
        """Run the console's own new code in this same process.

        Only once the new files have settled and no scheduler is being stopped.
        The schedulers keep running: the new code takes them back (see
        :meth:`_adopt`) and restarts each between rounds. True when tried.
        """
        now = self.clock() if now is None else now
        code = self._code_seen
        if (
            self._own_code is None
            or code is None
            or code == self._own_code
            or now - self._code_since < CODE_SETTLE
            or now < self._next_replace
            or any(slot.stopping or slot.reloading for slot in self.slots.values())
            or not self.check_code(code)
        ):
            return False
        self._next_replace = now + REPLACE_RETRY
        handover = {
            'pid': os.getpid(),
            'slots': {
                name: {
                    'pid': slot.process.pid, 'started': slot.started,
                    'started_at': slot.started_at, 'digest': slot.digest, 'code': slot.code,
                    'failures': slot.failures, 'restarts': slot.restarts,
                    'reloads': slot.reloads, 'last_exit': slot.last_exit,
                }
                for name, slot in self.slots.items()
                if slot.alive()
            },
        }  # fmt: skip
        try:
            self._write_private(self.handover_path, handover)
        except OSError:
            return True
        awake, self._awake = self._awake, None
        if awake is not None:
            awake.terminate()  # the new code starts its own
        # Helpers hold no round: stop them, and the new code starts them on its code.
        self.stop_helpers()
        self._say('总控换用新代码（同一进程），接管各项目调度')
        argv = [
            sys.executable,
            '-B',
            str(self.entry),
            'console',
            '--registry',
            str(self.registry_path),
        ]
        try:
            self.execv(argv[0], argv)
        except OSError:
            pass
        # Still here: the new code did not start. Keep going on the old code.
        self._awake = self._stay_awake()
        try:
            self.handover_path.unlink()
        except OSError:
            pass
        return True

    def _code_works(self, code) -> bool:
        """Whether the code on disk imports cleanly in a fresh interpreter (once per code)."""
        if self._code_checked is not None and self._code_checked[0] == code:
            return self._code_checked[1]
        modules = (
            'import autocontrol.cli, autocontrol.console, autocontrol.runner, autocontrol.service, '
            'autocontrol.dashboard.server'
        )
        try:
            done = subprocess.run(
                [self.python, '-B', '-c', modules], cwd=str(self.entry.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60,
            )  # fmt: skip
            works = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            works = False
        self._code_checked = (code, works)
        return works

    def _adopt(self) -> None:
        """Take back the schedulers this same process ran before it replaced its code."""
        path = self.handover_path
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        if type(data) is not dict or data.get('pid') != os.getpid():
            return  # left by another process: never touch its children
        saved_slots = data.get('slots')
        for name, saved in saved_slots.items() if type(saved_slots) is dict else ():
            slot = self.slots.get(name)
            if slot is None or slot.process is not None or type(saved) is not dict:
                continue
            process = Adopted(saved.get('pid'))
            if process.poll() is not None:
                continue  # gone, or not this process's child
            slot.process = process
            for key in ('started', 'started_at', 'digest', 'code', 'last_exit'):
                setattr(slot, key, saved.get(key))
            for key in ('failures', 'restarts', 'reloads'):
                value = saved.get(key)
                setattr(slot, key, value if type(value) is int else 0)
            slot.state = 'running'
            self._say(f'{name}：接管调度，pid {process.pid}')
            if slot.project is None:
                try:
                    slot.project = self.loader(slot.entry)
                except Exception:
                    pass  # without its view the scheduler is only not reloaded
