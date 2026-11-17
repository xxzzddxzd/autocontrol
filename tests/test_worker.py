"""Child streaming (ported from yslzm/tests/test_daily_board_runtime.py)."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from autocontrol.worker import MAX_OUTPUT, last_json_line, run_streamed

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
