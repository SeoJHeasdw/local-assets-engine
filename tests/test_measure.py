import sys
import os
import errno
import threading
import time

import pytest

from local_assets_engine.measure import (
    StageCancelled, StageFailed, failure_detail, is_time_report, parse_progress, parse_time_report,
    run_measured, was_killed,
)
from local_assets_engine import measure


def test_parse_progress_reads_marker_and_tqdm_bar():
    assert parse_progress('@@progress {"done": 1, "total": 4, "detail": "a.png"}') == (0.25, "a.png")
    assert parse_progress("Sampling:  42%|####      | 5/12") == (0.42, None)
    assert parse_progress("@@progress {broken") is None
    assert parse_progress("plain log line") is None


def test_parse_time_report_extracts_peak_and_rss():
    lines = [
        "        1.20 real         0.50 user         0.10 sys",
        "            52412416  maximum resident set size",
        "            40009920  peak memory footprint",
    ]
    assert all(is_time_report(line) for line in lines)
    assert parse_time_report(lines) == (40009920, 52412416)


def test_run_measured_streams_lines_progress_and_memory():
    lines, progress = [], []
    code = (
        "import sys; print('hello'); print('@@progress {\"done\": 1, \"total\": 2}', flush=True);"
        "sys.stderr.write('50%|##\\r75%|###\\n')"
    )
    result = run_measured(
        [sys.executable, "-c", code], capture_stdout=True,
        on_line=lambda _stream, line: lines.append(line),
        on_progress=lambda fraction, _detail: progress.append(fraction),
    )
    assert "hello" in lines and "hello" in result.stdout
    assert sorted(progress) == [0.5, 0.5, 0.75]
    assert not any(is_time_report(line) for line in lines)
    assert result.peak_memory_bytes and result.peak_memory_bytes > 0


def test_run_measured_failure_keeps_exit_code_and_last_line():
    with pytest.raises(StageFailed) as info:
        run_measured([sys.executable, "-c", "import sys; print('boom detail', file=sys.stderr); sys.exit(3)"])
    assert info.value.code == 3
    assert "boom detail" in str(info.value)


def test_failed_runs_still_carry_their_measurement():
    with pytest.raises(StageFailed) as info:
        run_measured([sys.executable, "-c", "import sys; sys.exit(2)"])
    assert info.value.peak_memory_bytes and info.value.peak_memory_bytes > 0


def test_run_measured_cancel_stops_the_process_group():
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    started = time.monotonic()
    with pytest.raises(StageCancelled):
        run_measured([sys.executable, "-c", "import time; time.sleep(30)"], cancel=cancel)
    assert time.monotonic() - started < 10


def test_failure_detail_prefers_the_error_over_a_trailing_warning():
    lines = ["Loading pipeline", "RuntimeError: out of memory", "  warnings.warn('resource_tracker: ...')"]
    assert failure_detail(lines) == "RuntimeError: out of memory"
    assert failure_detail(["plain line", "last line"]) == "last line"
    assert failure_detail([]) == ""


def test_a_process_the_os_killed_is_reported_as_killed():
    assert was_killed(StageFailed("x", code=1, tail=["time: signal: Invalid argument"]))
    assert not was_killed(StageFailed("x", code=1, tail=["모델 파일이 없습니다"]))


def test_cancel_escalates_to_stubborn_child_and_keeps_measurement(tmp_path, monkeypatch):
    monkeypatch.setattr(measure, "FORCE_KILL_AFTER_S", 0.1)
    marker = tmp_path / "pid"
    cancel = threading.Event()
    code = ("import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)")
    def trigger():
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()
    trigger_thread = threading.Thread(target=trigger)
    trigger_thread.start()
    with pytest.raises(StageCancelled) as info:
        run_measured([sys.executable, "-c", code], cancel=cancel)
    trigger_thread.join()
    assert info.value.launched and info.value.seconds < 5
    assert info.value.peak_memory_bytes > 0
    assert info.value.measurement_unavailable_reason is None
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_output_callback_failure_drains_pipes_and_stops_child(tmp_path):
    marker = tmp_path / "finished"
    code = ("import os,pathlib; print('first',flush=True); os.write(1,b'x\\n'*1000000); "
            f"pathlib.Path({str(marker)!r}).write_text('finished')")
    def disk_full(_stream, _line):
        raise OSError(errno.ENOSPC, "injected full log disk")
    started = time.monotonic()
    with pytest.raises(StageFailed, match="injected full log disk") as info:
        run_measured([sys.executable, "-c", code], on_line=disk_full)
    assert time.monotonic() - started < 5
    assert info.value.peak_memory_bytes > 0
    assert not marker.exists()


def test_missing_time_never_launches_unmeasured_command(tmp_path, monkeypatch):
    monkeypatch.setattr(measure, "TIME_BIN", tmp_path / "missing-time")
    marker = tmp_path / "launched"
    with pytest.raises(StageFailed) as info:
        run_measured([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"])
    assert not marker.exists() and not info.value.launched
    assert info.value.measurement_unavailable_reason == "measurement-tool-missing"


def test_parent_exit_does_not_leave_unwaited_descendant_running(tmp_path, monkeypatch):
    monkeypatch.setattr(measure, "FORCE_KILL_AFTER_S", 0.1)
    marker = tmp_path / "descendant.pid"
    child = ("import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
             f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)")
    parent = (f"import subprocess,sys,pathlib,time; subprocess.Popen([sys.executable,'-c',{child!r}]); "
              f"marker=pathlib.Path({str(marker)!r}); "
              "\nwhile not marker.exists(): time.sleep(.01)\n")
    with pytest.raises(StageFailed, match="하위 프로세스가 남았습니다"):
        run_measured([sys.executable, "-c", parent])
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_pump_thread_start_failure_also_cleans_launched_child(tmp_path, monkeypatch):
    marker = tmp_path / "pid"
    code = f"import os,time,pathlib; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
    def unavailable_thread(_self):
        raise RuntimeError("injected pump start failure")
    monkeypatch.setattr(measure.threading.Thread, "start", unavailable_thread)
    started = time.monotonic()
    with pytest.raises(StageFailed, match="injected pump start failure") as info:
        run_measured([sys.executable, "-c", code])
    assert time.monotonic() - started < 5
    assert info.value.launched and info.value.peak_memory_bytes > 0
    if marker.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)


def test_normal_descendant_teardown_keeps_successful_stage(tmp_path):
    marker = tmp_path / "child.pid"
    child = f"import os,time,pathlib; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(.05)"
    parent = (f"import subprocess,sys,pathlib,time; subprocess.Popen([sys.executable,'-c',{child!r}]); "
              f"marker=pathlib.Path({str(marker)!r}); "
              "\nwhile not marker.exists(): time.sleep(.01)\n")
    result = run_measured([sys.executable, "-c", parent])
    assert result.peak_memory_bytes > 0
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
