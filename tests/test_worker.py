"""Child streaming (ported from yslzm/tests/test_daily_board_runtime.py)."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from autocontrol.worker import MAX_OUTPUT, MAX_STDERR, last_json_line, run_streamed

ENV = 'TEST_PROGRESS_FD'


def test_stream_delivers_events_and_keeps_only_bounded_output(tmp_path):
    events = []
    script = tmp_path / 'worker.py'
    script.write_text(
        'import os, json, sys\n'
        f'fd=int(os.environ["{ENV}"])\n'
        "os.write(fd,b'not json\\n')\n"
        'os.write(fd,json.dumps({"kind":"task_started","task_id":"cardbook"}).encode()+b"\\n")\n'
        'sys.stdout.write("x"*3000000+"\\n")\n'
        'print(json.dumps({"execution":"complete"}))\n'
    )
    result = run_streamed([sys.executable, '-B', str(script)], cwd=tmp_path, on_event=events.append,
                          progress_env=ENV, timeout=10)  # fmt: skip
    assert events == [{'kind': 'task_started', 'task_id': 'cardbook'}]
    assert result.returncode == 0
    assert len(result.stdout) <= MAX_OUTPUT
    assert last_json_line(result.stdout) == {'execution': 'complete'}
    assert [p.name for p in Path(tmp_path).iterdir()] == ['worker.py']


def test_reporter_failure_cannot_abort_the_worker(tmp_path):
    script = tmp_path / 'worker.py'
    script.write_text(
        f'import os\nos.write(int(os.environ["{ENV}"]),b\'{{"kind":"plan"}}\\n\')\nprint("done")\n'
    )

    def broken(_):
        raise RuntimeError('private diagnostic')

    result = run_streamed([sys.executable, '-B', str(script)], cwd=tmp_path, on_event=broken,
                          progress_env=ENV, timeout=10)  # fmt: skip
    assert result.returncode == 0


def test_timeout_terminates_the_child(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        run_streamed([sys.executable, '-B', '-c', 'import time;time.sleep(20)'], cwd=tmp_path,
                     on_event=lambda _: None, progress_env=ENV, timeout=0.2)  # fmt: skip


def test_unread_stdin_cannot_bypass_the_timeout(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        run_streamed([sys.executable, '-B', '-c', 'import time;time.sleep(20)'], cwd=tmp_path,
                     input=b'x' * 131072, on_event=lambda _: None, progress_env=ENV, timeout=0.2)  # fmt: skip


def test_last_json_line_accepts_only_a_final_object():
    assert last_json_line(b'noise\n{"a": 1}\n') == {'a': 1}
    assert last_json_line(b'{"a": 1}\nnoise\n') is None
    assert last_json_line(json.dumps([1]).encode()) is None
    assert last_json_line(b'') is None


def test_stdout_lines_arrive_while_running_and_restart_the_timeout(tmp_path):
    script = (
        'import time\nfor n in range(3):\n    print("line", n, flush=True)\n    time.sleep(0.4)\n'
    )
    seen = []
    result = run_streamed(
        [sys.executable, '-B', '-c', script],
        cwd=tmp_path,
        on_event=lambda event: None,
        progress_env=ENV,
        timeout=0.9,
        on_line=seen.append,
        restart_timeout_on_line=True,
    )
    assert result.returncode == 0
    assert seen == [b'line 0', b'line 1', b'line 2']


def test_without_restart_the_timeout_bounds_the_whole_child(tmp_path):
    script = 'import time\nfor n in range(3):\n    print(n, flush=True)\n    time.sleep(0.4)\n'
    with pytest.raises(subprocess.TimeoutExpired):
        run_streamed(
            [sys.executable, '-B', '-c', script],
            cwd=tmp_path,
            on_event=lambda event: None,
            progress_env=ENV,
            timeout=0.9,
            on_line=lambda line: None,
        )


def run_python(tmp_path, script, **options):
    options.setdefault('timeout', 10)
    return run_streamed([sys.executable, '-B', '-c', script], cwd=tmp_path,
                        on_event=lambda _: None, progress_env=ENV, **options)  # fmt: skip


def test_the_stderr_tail_reaches_its_sink_once_the_child_ends(tmp_path):
    kept = []
    script = (
        'import sys\nsys.stderr.write("x" * 40000)\nsys.stderr.write("Error: boom\\n")\nprint(1)\n'
    )
    result = run_python(tmp_path, script, on_stderr=kept.append)
    assert result.returncode == 0 and result.stdout.strip() == b'1'
    assert len(kept) == 1 and len(kept[0]) == MAX_STDERR
    assert kept[0].endswith(b'Error: boom\n')


def test_a_quiet_child_never_calls_the_sink(tmp_path):
    kept = []
    run_python(tmp_path, 'print(1)', on_stderr=kept.append)
    assert kept == []


def test_the_stderr_tail_is_kept_after_a_timeout(tmp_path):
    kept = []
    script = 'import sys, time\nsys.stderr.write("still working\\n")\nsys.stderr.flush()\ntime.sleep(20)\n'
    with pytest.raises(subprocess.TimeoutExpired):
        run_python(tmp_path, script, timeout=1.5, on_stderr=kept.append)
    assert kept == [b'still working\n']


def test_a_helper_holding_stderr_open_does_not_delay_the_end(tmp_path):
    kept = []
    script = (
        'import subprocess, sys\n'
        'helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],'
        ' stdout=subprocess.DEVNULL)\n'
        'sys.stderr.write("left a helper\\n")\n'
        'print(helper.pid)\n'
    )
    started = time.monotonic()
    result = run_python(tmp_path, script, timeout=20, on_stderr=kept.append)
    try:
        assert time.monotonic() - started < 10
        assert kept == [b'left a helper\n']
    finally:
        os.kill(int(result.stdout.split()[-1]), signal.SIGTERM)
