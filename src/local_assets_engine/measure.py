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
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

TIME_BIN = Path("/usr/bin/time")
PROGRESS_PREFIX = "@@progress "
FORCE_KILL_AFTER_S = 3.0
CHILD_EXIT_GRACE_S = 0.25
MAX_LINE_BYTES = 65536
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
# Keep time alive to reap its direct child and print resource measurements.
# The child becomes a separate session before exec, and publishes its group id.
_SESSION_EXEC = (
    "import os,sys; os.setsid(); fd=int(sys.argv[1]); "
    "os.write(fd,str(os.getpid()).encode()); os.close(fd); "
    "os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"
)

_PEAK = re.compile(r"^\s*(\d+)\s+peak memory footprint\s*$")
_RSS = re.compile(r"^\s*(\d+)\s+maximum resident set size\s*$")
_TIME_SUMMARY = re.compile(r"^\s*\d+(?:\.\d+)?\s+real\s+\d+(?:\.\d+)?\s+user\s+")
_TIME_COUNTER = re.compile(r"^\s*\d+\s+[a-z][a-z ()/-]*$")
_TQDM_PERCENT = re.compile(r"(\d{1,3})%\|")
_LINE_BREAK = re.compile(rb"[\r\n]")


# 다른 앱이 GPU를 함께 쓰면 macOS 감시가 긴 Metal 명령을 끊는다. 기기 문제가 아니라
# 그때의 혼잡이므로 한 번은 다시 해 볼 값어치가 있다.
GPU_BUSY_SIGNATURES = (
    "kIOGPUCommandBufferCallbackErrorTimeout",
    "ImpactingInteractivity",
    "Command buffer execution failed",
    "Insufficient Memory",
)


# 실패한 실행의 마지막 줄은 경고이기 쉽다. 오류로 보이는 줄을 뒤에서부터 먼저 찾는다.
ERROR_MARKERS = ("Error", "error:", "Exception", "Traceback", "Killed", "signal:", "실패")
# /usr/bin/time은 자식이 신호로 죽으면 이렇게 적는다. 이 기기에서는 메모리 부족이 원인이다.
KILLED_MARKERS = ("time: signal:", "Killed", "signal: killed")


class StageCancelled(Exception):
    """The user stopped the job while this stage was running."""

    def __init__(self, *, seconds: float | None = None, peak_memory_bytes: int | None = None,
                 max_rss_bytes: int | None = None, code: int | None = None,
                 launched: bool = False, measurement_unavailable_reason: str | None = None):
        super().__init__()
        self.seconds = seconds
        self.peak_memory_bytes = peak_memory_bytes
        self.max_rss_bytes = max_rss_bytes
        self.code = code
        self.launched = launched
        self.measurement_unavailable_reason = measurement_unavailable_reason


class StageFailed(Exception):
    # 실패한 실행의 측정치도 남긴다. 메모리가 모자라 끊긴 경우 그 수치가 곧 근거다.
    def __init__(self, message: str, *, code: int | None, tail: list[str],
                 peak_memory_bytes: int | None = None, max_rss_bytes: int | None = None,
                 seconds: float | None = None, launched: bool = True,
                 measurement_unavailable_reason: str | None = None):
        super().__init__(message)
        self.code = code
        self.tail = tail
        self.peak_memory_bytes = peak_memory_bytes
        self.max_rss_bytes = max_rss_bytes
        self.seconds = seconds
        self.launched = launched
        self.measurement_unavailable_reason = measurement_unavailable_reason


@dataclass(frozen=True)
class StageResult:
    seconds: float
    peak_memory_bytes: int | None
    max_rss_bytes: int | None
    stdout: str
    measurement_unavailable_reason: str | None = None


def is_gpu_busy(failure: "StageFailed") -> bool:
    text = "\n".join([*failure.tail, str(failure)])
    return any(signature in text for signature in GPU_BUSY_SIGNATURES)


def failure_detail(lines: Sequence[str]) -> str:
    """The most informative line of a failed run."""
    candidates = [line.strip() for line in lines if line.strip() and parse_progress(line) is None]
    for line in reversed(candidates):
        if any(marker in line for marker in ERROR_MARKERS):
            return line
    return candidates[-1] if candidates else ""


def was_killed(failure: "StageFailed") -> bool:
    """True when the operating system killed the process instead of it exiting."""
    return any(marker in "\n".join(failure.tail) for marker in KILLED_MARKERS)


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


def parse_time_report(lines: Iterable[str]) -> tuple[int | None, int | None]:
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
        # A tool without newlines must not grow the engine's buffer indefinitely.
        while len(buffer) >= MAX_LINE_BYTES:
            on_line(buffer[:MAX_LINE_BYTES].decode("utf-8", "replace"))
            buffer = buffer[MAX_LINE_BYTES:]
    if buffer.strip():
        on_line(buffer.decode("utf-8", "replace"))


def _signal_group(group: int, sig: int) -> None:
    try:
        os.killpg(group, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_measured(
    args: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    cancel: threading.Event | None = None,
    on_line: Callable[[str, str], None] | None = None,
    on_progress: Callable[[float, str | None], None] | None = None,
    capture_stdout: bool = False,
    lease_fds: Sequence[int] = (),
) -> StageResult:
    command = [str(part) for part in args]
    if not TIME_BIN.is_file():
        raise StageFailed("측정 도구 /usr/bin/time이 없습니다.", code=None, tail=[],
                          launched=False, measurement_unavailable_reason="measurement-tool-missing")
    if cancel is not None and cancel.is_set():
        raise StageCancelled()

    started = time.monotonic()
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    try:
        proc = subprocess.Popen(
            [str(TIME_BIN), "-l", sys.executable, "-c", _SESSION_EXEC, str(write_fd), *command],
            cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, pass_fds=(write_fd, *lease_fds),
        )
    except OSError as error:
        os.close(read_fd)
        raise StageFailed(str(error), code=None, tail=[], seconds=round(time.monotonic() - started, 2),
                          launched=False, measurement_unavailable_reason="process-not-started") from error
    finally:
        os.close(write_fd)
    tail: collections.deque[str] = collections.deque(maxlen=60)
    report: collections.deque[str] = collections.deque(maxlen=80)
    stdout_parts: list[str] = []
    capture_bytes = 0
    errors: list[Exception] = []
    lock = threading.Lock()

    def pump_error(error: Exception) -> None:
        with lock:
            if not errors:
                errors.append(error)

    def handle(stream_name: str, line: str) -> None:
        nonlocal capture_bytes
        with lock:
            if stream_name == "stderr" and is_time_report(line):
                report.append(line)
                return
            if stream_name == "stdout" and capture_stdout:
                capture_bytes += len(line.encode("utf-8"))
                if capture_bytes <= MAX_CAPTURE_BYTES:
                    stdout_parts.append(line)
                elif not errors:
                    errors.append(ValueError("단계 표준 출력이 16MB 한도를 넘었습니다."))
            progress = parse_progress(line)
            if progress is None or not line.startswith(PROGRESS_PREFIX):
                tail.append(line[-4096:])
            # A callback failure is delivered to the waiting thread. Keep draining
            # both pipes, including time's final report, while cleanup runs.
            if not errors:
                try:
                    if on_line:
                        on_line(stream_name, line)
                    if progress is not None and on_progress:
                        on_progress(*progress)
                except Exception as error:  # noqa: BLE001 - propagate across threads
                    errors.append(error)

    def pump(stream, name: str) -> None:
        try:
            _pump(stream, lambda line: handle(name, line))
        except Exception as error:  # noqa: BLE001 - propagate stream I/O failures
            pump_error(error)

    pumps = [threading.Thread(target=pump, args=(stream, name), daemon=True)
             for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr"))]
    group: int | None = None
    cancelled = False
    stopping = False
    kill_deadline: float | None = None
    descendant_exit_deadline: float | None = None
    try:
        for worker in pumps:
            worker.start()
        while True:
            if group is None:
                try:
                    value = os.read(read_fd, 64)
                    if value:
                        group = int(value)
                except BlockingIOError:
                    pass
            code = proc.poll()
            if cancel is not None and cancel.is_set():
                cancelled = True
            live_children = group is not None and _group_alive(group)
            if code is not None and live_children and not stopping:
                # Resource trackers can exit just after their parent. Give that
                # normal teardown a short grace period while retaining the lane.
                if descendant_exit_deadline is None:
                    descendant_exit_deadline = time.monotonic() + CHILD_EXIT_GRACE_S
                elif time.monotonic() >= descendant_exit_deadline:
                    pump_error(RuntimeError("단계 종료 뒤 하위 프로세스가 남았습니다."))
            if group is None and code is None and time.monotonic() - started > 5:
                pump_error(RuntimeError("단계 프로세스 시작 확인이 지연됐습니다."))
            if (cancelled or errors) and not stopping and (group is not None or code is not None):
                stopping = True
                if group is not None:
                    _signal_group(group, signal.SIGTERM)
                kill_deadline = time.monotonic() + FORCE_KILL_AFTER_S
            if group is None and errors and code is None:
                _signal_group(proc.pid, signal.SIGKILL)
            if kill_deadline is not None and time.monotonic() >= kill_deadline:
                if group is not None:
                    _signal_group(group, signal.SIGKILL)
                kill_deadline = time.monotonic() + 0.2
            if code is not None and not live_children:
                break
            time.sleep(0.05)
        for worker in pumps:
            worker.join()
    except Exception as error:  # noqa: BLE001 - controller/thread-start failures also clean up
        pump_error(error)
    finally:
        if group is None:
            # A thread-start failure can happen before the exec handshake was
            # read. Wait for that tiny setup step before signalling descendants.
            deadline = time.monotonic() + 5
            while group is None and proc.poll() is None and time.monotonic() < deadline:
                try:
                    value = os.read(read_fd, 64)
                    if value:
                        group = int(value)
                except BlockingIOError:
                    pass
                if group is None:
                    time.sleep(0.01)
        os.close(read_fd)
        # Even exceptions in this controller must not release the lane with
        # a live child group. The time wrapper remains alive to reap its child.
        if group is not None:
            _signal_group(group, signal.SIGKILL)
        if proc.poll() is None:
            if group is None:
                _signal_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _signal_group(proc.pid, signal.SIGKILL)
                proc.wait()
        for worker, stream, name in zip(pumps, (proc.stdout, proc.stderr), ("stdout", "stderr")):
            if worker.ident is not None:
                worker.join()
            else:
                # Recover the time report when starting its pump failed.
                pump(stream, name)
        for stream in (proc.stdout, proc.stderr):
            stream.close()

    code = proc.returncode
    seconds = round(time.monotonic() - started, 2)
    peak, rss = parse_time_report(report)
    missing = None if peak is not None else "time-report-missing"
    if errors:
        raise StageFailed(f"단계 출력 처리 실패: {errors[0]}", code=code, tail=list(tail),
                          seconds=seconds, peak_memory_bytes=peak, max_rss_bytes=rss,
                          measurement_unavailable_reason=missing) from errors[0]
    if cancelled:
        raise StageCancelled(seconds=seconds, code=code, launched=True,
                             peak_memory_bytes=peak, max_rss_bytes=rss,
                             measurement_unavailable_reason=missing)
    if code != 0:
        lines = list(tail)
        detail = failure_detail(lines)
        raise StageFailed(
            f"종료 코드 {code}" + (f": {detail[-600:]}" if detail else ""),
            code=code, tail=lines, seconds=seconds, peak_memory_bytes=peak, max_rss_bytes=rss,
            measurement_unavailable_reason=missing,
        )
    return StageResult(seconds=seconds, peak_memory_bytes=peak, max_rss_bytes=rss,
                       stdout="\n".join(stdout_parts), measurement_unavailable_reason=missing)
