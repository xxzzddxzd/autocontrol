"""Run the game worker as a child process and stream its progress events.

Progress arrives as JSON lines on an inherited pipe whose descriptor number is
passed in an environment variable. Events are parsed in memory and handed to a
callback; stdout is kept in a bounded buffer (the last line is the worker's
summary). stderr is discarded unless the caller asks for its bounded tail
(``on_stderr``), which it may keep for people to diagnose a failure. A failing
callback can never abort the worker.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time

MAX_EVENT = 64 * 1024
MAX_OUTPUT = 2 * 1024 * 1024 + 1
MAX_STDERR = 16 * 1024


def _keep_tail(tail: bytearray, chunk: bytes) -> None:
    tail.extend(chunk)
    if len(tail) > MAX_STDERR:
        del tail[:-MAX_STDERR]


def _drain(fd: int, tail: bytearray) -> None:
    """Read what a pipe already holds without waiting for its writer to close it."""
    os.set_blocking(fd, False)
    try:
        while chunk := os.read(fd, 65536):
            _keep_tail(tail, chunk)
    except BlockingIOError:
        pass


def _stop(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def run_streamed(
    argv,
    *,
    cwd,
    on_event,
    progress_env,
    timeout=None,
    input=None,
    env=None,
    raw=False,
    on_line=None,
    restart_timeout_on_line=False,
    on_stderr=None,
):
    """Run ``argv``; return ``CompletedProcess`` whose stdout is the bounded tail.

    With ``raw=True`` the callback receives each event line as bytes (``None``
    for a line over :data:`MAX_EVENT`) so a caller can validate and count it.
    ``on_line`` receives every complete stdout line as bytes while the child
    runs; with ``restart_timeout_on_line`` each such line restarts ``timeout``
    (a multi-phase worker prints one line per finished phase). ``on_stderr``
    receives the last :data:`MAX_STDERR` bytes the child wrote to stderr once
    it has ended, also after a timeout, and only when there were any.
    """
    read_fd, write_fd = os.pipe()
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', **(env or {}), progress_env: str(write_fd)}
    child = None
    output = bytearray()
    errors = bytearray() if on_stderr is not None else None
    pending = bytearray()
    dropping = False
    clock = {'deadline': time.monotonic() + timeout if timeout is not None else None}
    line = bytearray()

    def expired() -> bool:
        return clock['deadline'] is not None and time.monotonic() >= clock['deadline']

    def stdout_lines(chunk: bytes) -> None:
        for part in chunk.splitlines(keepends=True):
            line.extend(part)
            if len(line) > MAX_OUTPUT:
                line.clear()  # an unbounded line is never a phase summary
                continue
            if part.endswith(b'\n'):
                try:
                    on_line(bytes(line).rstrip(b'\r\n'))
                except Exception:
                    pass  # a failing callback never interrupts the worker
                line.clear()
                if restart_timeout_on_line and timeout is not None:
                    clock['deadline'] = time.monotonic() + timeout

    def give_up():
        _stop(child)
        raise subprocess.TimeoutExpired(argv, timeout, output=bytes(output))

    try:
        child = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            pass_fds=(write_fd,),
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if errors is not None else subprocess.DEVNULL,
        )
        os.close(write_fd)
        write_fd = None
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ, 'event')
            selector.register(child.stdout, selectors.EVENT_READ, 'stdout')
            if errors is not None:
                selector.register(child.stderr, selectors.EVENT_READ, 'stderr')
            remaining_input = memoryview(input) if input is not None else None
            if remaining_input is not None:
                os.set_blocking(child.stdin.fileno(), False)
                if remaining_input:
                    selector.register(child.stdin, selectors.EVENT_WRITE, 'stdin')
                else:
                    child.stdin.close()
            while selector.get_map():
                if expired():
                    give_up()
                if (
                    errors is not None
                    and {key.data for key in selector.get_map().values()} == {'stderr'}
                    and child.poll() is not None
                ):
                    # The worker is gone; a helper it left behind may hold its
                    # stderr open. Keep what is already written, never wait.
                    _drain(child.stderr.fileno(), errors)
                    break
                for key, _ in selector.select(0.25):
                    if key.data == 'stdin':
                        try:
                            written = os.write(key.fd, remaining_input[:65536])
                            remaining_input = remaining_input[written:]
                        except BrokenPipeError:
                            remaining_input = remaining_input[:0]
                        if not remaining_input:
                            selector.unregister(key.fileobj)
                            child.stdin.close()
                        continue
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == 'stderr':
                        _keep_tail(errors, chunk)
                        continue
                    if key.data == 'stdout':
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT:
                            del output[:-MAX_OUTPUT]
                        if on_line is not None:
                            stdout_lines(chunk)
                        continue
                    for part in chunk.splitlines(keepends=True):
                        if not dropping:
                            pending.extend(part)
                            if len(pending) > MAX_EVENT:
                                pending.clear()
                                dropping = True
                        if part.endswith(b'\n'):
                            try:
                                if raw:
                                    on_event(None if dropping else bytes(pending).rstrip(b'\r\n'))
                                elif not dropping:
                                    event = json.loads(pending)
                                    if type(event) is dict:
                                        on_event(event)
                            except Exception:
                                # Malformed events and display failures never
                                # interrupt the worker.
                                pass
                            pending.clear()
                            dropping = False
        # Both streams are closed; the process normally exits right away.
        while True:
            try:
                code = child.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if expired():
                    give_up()
        return subprocess.CompletedProcess(argv, code, stdout=bytes(output))
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if child is not None:
            _stop(child)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
        if errors:
            try:
                on_stderr(bytes(errors))
            except Exception:
                pass  # diagnostics never affect the phase


def last_json_line(output, limit: int = 2 * 1024 * 1024):
    """Parse the final stdout line as a JSON object; anything else is ``None``."""
    if type(output) is str:
        output = output.encode('utf-8')
    if type(output) is not bytes or not output:
        return None
    tail = output[-(limit + 1) :].rstrip()
    line = tail.rsplit(b'\n', 1)[-1]
    if len(line) > limit or (len(output) > limit and line == tail):
        return None
    try:
        value = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return value if type(value) is dict else None
