"""Run one stage process, stream its output, and measure what it cost.

Every heavy step (image model, background removal, TRELLIS, Blender) runs as its
own process under ``/usr/bin/time -l``. The per-stage wall time and peak memory
footprint are the evidence for what this Mac can and cannot do.
"""

from __future__ import annotations

import collections
import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

TIME_BIN = Path("/usr/bin/time")
PROGRESS_PREFIX = "@@progress "
FORCE_KILL_AFTER_S = 3.0

_PEAK = re.compile(r"^\s*(\d+)\s+peak memory footprint\s*$")
_RSS = re.compile(r"^\s*(\d+)\s+maximum resident set size\s*$")
_TIME_SUMMARY = re.compile(r"^\s*\d+(?:\.\d+)?\s+real\s+\d+(?:\.\d+)?\s+user\s+")
_TIME_COUNTER = re.compile(r"^\s*\d+\s+[a-z][a-z ()/-]*$")
_TQDM_PERCENT = re.compile(r"(\d{1,3})%\|")
_LINE_BREAK = re.compile(rb"[\r\n]")


class StageCancelled(Exception):
    """The user stopped the job while this stage was running."""


class StageFailed(Exception):
    # 실패한 실행의 측정치도 남긴다. 메모리가 모자라 끊긴 경우 그 수치가 곧 근거다.
    def __init__(self, message: str, *, code: int | None, tail: list[str],
                 peak_memory_bytes: int | None = None, max_rss_bytes: int | None = None):
        super().__init__(message)
        self.code = code
        self.tail = tail
        self.peak_memory_bytes = peak_memory_bytes
        self.max_rss_bytes = max_rss_bytes


@dataclass(frozen=True)
class StageResult:
    seconds: float
    peak_memory_bytes: int | None
    max_rss_bytes: int | None
    stdout: str


def parse_progress(line: str) -> tuple[float, str | None] | None:
    """Return (fraction, detail) from our JSON marker or a tqdm bar."""
    if line.startswith(PROGRESS_PREFIX):
        try:
            data = json.loads(line[len(PROGRESS_PREFIX):])
            total = float(data.get("total") or 0)
            done = float(data.get("done") or 0)
        except (ValueError, TypeError, AttributeError):
            return None
        if total <= 0:
            return None
        return max(0.0, min(1.0, done / total)), data.get("detail")
    matches = _TQDM_PERCENT.findall(line)
    if matches:
        return max(0.0, min(1.0, int(matches[-1]) / 100)), None
    return None


def is_time_report(line: str) -> bool:
    return bool(_TIME_SUMMARY.match(line) or _TIME_COUNTER.match(line))


def parse_time_report(lines: Sequence[str]) -> tuple[int | None, int | None]:
    peak = rss = None
    for line in lines:
        if match := _PEAK.match(line):
            peak = int(match.group(1))
        elif match := _RSS.match(line):
            rss = int(match.group(1))
    return peak, rss


def _pump(stream, on_line: Callable[[str], None]) -> None:
    # tqdm은 줄바꿈 없이 \r로 막대를 다시 그린다. 두 문자 모두 줄 경계로 본다.
    buffer = b""
    reader = getattr(stream, "read1", stream.read)
    while chunk := reader(65536):
        buffer += chunk
        *parts, buffer = _LINE_BREAK.split(buffer)
        for part in parts:
            if part.strip():
                on_line(part.decode("utf-8", "replace"))
    if buffer.strip():
        on_line(buffer.decode("utf-8", "replace"))


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_measured(
    args: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    cancel: threading.Event | None = None,
    on_line: Callable[[str, str], None] | None = None,
    on_progress: Callable[[float, str | None], None] | None = None,
    capture_stdout: bool = False,
) -> StageResult:
    command = [str(part) for part in args]
    measured = TIME_BIN.exists()
    if measured:
        command = [str(TIME_BIN), "-l", *command]
    if cancel is not None and cancel.is_set():
        raise StageCancelled()

    started = time.monotonic()
    proc = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    tail: collections.deque[str] = collections.deque(maxlen=60)
    report: list[str] = []
    stdout_parts: list[str] = []
    lock = threading.Lock()

    def handle(stream_name: str, line: str) -> None:
        with lock:
            if stream_name == "stderr" and measured and is_time_report(line):
                report.append(line)
                return
            if stream_name == "stdout" and capture_stdout:
                stdout_parts.append(line)
            progress = parse_progress(line)
            if progress is None or not line.startswith(PROGRESS_PREFIX):
                tail.append(line)
            if on_line:
                on_line(stream_name, line)
            if progress is not None and on_progress:
                on_progress(*progress)

    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, lambda l: handle("stdout", l)), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, lambda l: handle("stderr", l)), daemon=True),
    ]
    for pump in pumps:
        pump.start()

    cancelled = False
    kill_deadline: float | None = None
    while True:
        try:
            code = proc.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set() and not cancelled:
            cancelled = True
            _signal_group(proc, signal.SIGTERM)
            kill_deadline = time.monotonic() + FORCE_KILL_AFTER_S
        if kill_deadline is not None and time.monotonic() > kill_deadline:
            _signal_group(proc, signal.SIGKILL)
            kill_deadline = None
    for pump in pumps:
        pump.join(timeout=5)

    if cancelled:
        raise StageCancelled()
    peak, rss = parse_time_report(report)
    if code != 0:
        lines = list(tail)
        detail = next((line for line in reversed(lines) if parse_progress(line) is None), "")
        raise StageFailed(
            f"종료 코드 {code}" + (f": {detail[-600:]}" if detail else ""),
            code=code, tail=lines, peak_memory_bytes=peak, max_rss_bytes=rss,
        )
    return StageResult(
        seconds=round(time.monotonic() - started, 2),
        peak_memory_bytes=peak,
        max_rss_bytes=rss,
        stdout="\n".join(stdout_parts),
    )
