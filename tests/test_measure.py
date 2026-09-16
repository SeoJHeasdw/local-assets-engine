import sys
import threading
import time

import pytest

from local_assets_engine.measure import (
    StageCancelled, StageFailed, failure_detail, is_time_report, parse_progress, parse_time_report,
    run_measured, was_killed,
)


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
