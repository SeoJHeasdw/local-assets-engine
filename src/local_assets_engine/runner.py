"""Single-lane job runner.

Heavy models share 36GB of unified memory, so exactly one job runs at a time.
Each stage records wall time and the peak memory footprint of its processes.
"""

from __future__ import annotations

import collections
import os
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Sequence

from .jobs import JobNotFound, JobStore, now_iso
from .measure import StageCancelled, StageResult, parse_progress, run_measured
from .paths import child_env
from .presets import PresetError, load_presets

FLUSH_INTERVAL_S = 0.5
LOG_TAIL_LINES = 40
_DOWNLOAD_MARKERS = ("Fetching", "Downloading", "download")


class UnitTracker:
    """Turns repeated 0→100% bars (one per image or sampler) into one fraction."""

    def __init__(self, units: int):
        self.units = max(1, units)
        self.index = 0
        self.last = 0.0

    def update(self, fraction: float) -> float:
        if fraction + 0.5 < self.last and self.index < self.units - 1:
            self.index += 1
        self.last = fraction
        return min(1.0, (self.index + fraction) / self.units)


class Stage:
    def __init__(self, ctx: "JobContext", name: str, label: str):
        self.ctx = ctx
        self.name = name
        self.label = label
        self.fraction = 0.0
        self.detail: str | None = None
        self.processes: list[dict[str, Any]] = []
        self._started = 0.0
        self._last_flush = 0.0
        self._index = -1

    def __enter__(self) -> "Stage":
        self.ctx.check_cancel()
        self._started = time.monotonic()

        def add(job: dict[str, Any]) -> None:
            job["stages"].append({
                "name": self.name, "label": self.label, "state": "running",
                "startedAt": now_iso(), "progress": 0.0, "detail": None,
                "seconds": None, "peakMemoryBytes": None, "processes": [],
            })

        self._index = len(self.ctx.mutate(add)["stages"]) - 1
        return self

    def progress(self, fraction: float, detail: str | None = None, *, force: bool = False) -> None:
        self.fraction = max(self.fraction, min(1.0, max(0.0, fraction)))
        if detail:
            self.detail = detail
        now = time.monotonic()
        if force or now - self._last_flush >= FLUSH_INTERVAL_S:
            self._last_flush = now
            self._write(state="running")

    def run(
        self,
        args: Sequence[str | Path],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        units: int = 1,
        span: tuple[float, float] = (0.0, 1.0),
        interpret: Callable[[str], tuple[float | None, str | None] | None] | None = None,
        capture_stdout: bool = False,
    ) -> StageResult:
        tracker = UnitTracker(units)
        downloading = {"active": False}

        def scaled(fraction: float) -> float:
            return span[0] + (span[1] - span[0]) * fraction

        def on_line(_stream: str, line: str) -> None:
            self.ctx.log_line(line)
            downloading["active"] = any(marker in line for marker in _DOWNLOAD_MARKERS)
            if interpret is None:
                return
            result = interpret(line)
            if result is not None:
                fraction, detail = result
                self.progress(scaled(fraction) if fraction is not None else self.fraction, detail)

        def on_progress(fraction: float, detail: str | None) -> None:
            if downloading["active"]:
                self.progress(self.fraction, f"모델 파일 내려받는 중 {round(fraction * 100)}%")
            elif interpret is None:
                self.progress(scaled(tracker.update(fraction)), detail)

        result = run_measured(
            args, cwd=cwd, env=env or child_env(), cancel=self.ctx.cancel,
            on_line=on_line, on_progress=on_progress, capture_stdout=capture_stdout,
        )
        self.processes.append({
            "command": Path(str(args[0])).name if len(args) < 3 or str(args[1]) != "-m" else str(args[2]),
            "seconds": result.seconds,
            "peakMemoryBytes": result.peak_memory_bytes,
        })
        return result

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            state = "done"
            self.fraction = 1.0
        elif issubclass(exc_type, StageCancelled):
            state = "cancelled"
        else:
            state = "failed"
        self._write(state=state, final=True)
        self.ctx.flush_log()
        return False

    def _write(self, *, state: str, final: bool = False) -> None:
        def apply(job: dict[str, Any]) -> None:
            stage = job["stages"][self._index]
            stage.update(state=state, progress=round(self.fraction, 4), detail=self.detail)
            if final:
                peaks = [p["peakMemoryBytes"] for p in self.processes if p["peakMemoryBytes"]]
                stage.update(
                    seconds=round(time.monotonic() - self._started, 2),
                    peakMemoryBytes=max(peaks) if peaks else None,
                    processes=self.processes,
                )

        self.ctx.mutate(apply)


class JobContext:
    def __init__(self, store: JobStore, job_id: str, presets: dict[str, Any], cancel: threading.Event):
        self.store = store
        self.job_id = job_id
        self.presets = presets
        self.cancel = cancel
        self.dir = store.job_dir(job_id)
        self.params: dict[str, Any] = store.load(job_id)["params"]
        self._tail: collections.deque[str] = collections.deque(maxlen=LOG_TAIL_LINES)
        self._log_lock = threading.Lock()

    def stage(self, name: str, label: str) -> Stage:
        return Stage(self, name, label)

    def skip_stage(self, name: str, label: str, reason: str) -> None:
        def add(job: dict[str, Any]) -> None:
            job["stages"].append({
                "name": name, "label": label, "state": "skipped", "startedAt": now_iso(),
                "progress": 1.0, "detail": reason, "seconds": 0, "peakMemoryBytes": None, "processes": [],
            })

        self.mutate(add)

    def mutate(self, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        def apply(job: dict[str, Any]) -> None:
            fn(job)
            # 기록을 쓸 때마다 살아 있음을 남긴다. 다른 엔진이 시작해도 닫히지 않는다.
            if job.get("state") == "running" and isinstance(job.get("owner"), dict):
                job["owner"]["heartbeat"] = now_iso()

        return self.store.update(self.job_id, apply)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise StageCancelled()

    def log_line(self, line: str) -> None:
        # 진행 막대는 초당 수십 번 다시 그려진다. 기록에는 막대가 아닌 줄만 남긴다.
        if parse_progress(line) is not None and "|" in line:
            return
        with self._log_lock:
            self._tail.append(line[-500:])
            with (self.dir / "job.log").open("a", encoding="utf-8") as handle:
                handle.write(line.rstrip("\n") + "\n")

    def flush_log(self) -> None:
        with self._log_lock:
            tail = list(self._tail)
        self.mutate(lambda job: job.__setitem__("logTail", tail))

    def rel(self, path: str | Path) -> str:
        return Path(path).resolve().relative_to(self.dir.resolve()).as_posix()

    def add_asset(
        self, *, kind: str, role: str, file: str | Path,
        preview: str | Path | None = None, meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {}

        def add(job: dict[str, Any]) -> None:
            record.update({
                "id": f"a{len(job['assets']) + 1:02d}", "kind": kind, "role": role,
                "file": self.rel(file), "preview": self.rel(preview) if preview else None,
                "meta": meta or {}, "review": "pending", "createdAt": now_iso(),
            })
            job["assets"].append(dict(record))

        self.mutate(add)
        return record


class Runner:
    def __init__(self, store: JobStore, *, recipes: dict[str, Any] | None = None,
                 presets_loader: Callable[[], dict[str, Any]] = load_presets):
        if recipes is None:
            from .recipes import RECIPES
            recipes = RECIPES
        self.store = store
        self.recipes = recipes
        self.presets_loader = presets_loader
        self.current_job_id: str | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.store.recover_interrupted()
        self._thread = threading.Thread(target=self._loop, name="asset-runner", daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 8.0) -> None:
        """Stop the running job so its model processes do not outlive the engine."""
        with self._lock:
            events = list(self._cancels.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + timeout
        while self.current_job_id and time.monotonic() < deadline:
            time.sleep(0.1)

    def create(self, recipe_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
        recipe = self.recipes.get(recipe_id)
        if recipe is None:
            raise PresetError(f"알 수 없는 레시피입니다: {recipe_id}")
        normalized, title = recipe.prepare(dict(params or {}), self.presets_loader(), self.store)
        return self.store.create(recipe_id, normalized, title)

    def submit(self, recipe_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
        job = self.create(recipe_id, params)
        self._queue.put(job["id"])
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            def mark(job: dict[str, Any]) -> None:
                if job["state"] == "queued":
                    job.update(state="cancelled", finishedAt=now_iso())
                elif job["state"] == "running":
                    job["state"] = "cancelling"

            job = self.store.update(job_id, mark)
            if event := self._cancels.get(job_id):
                event.set()
            return job

    def run_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.load(job_id)
        if job["state"] != "queued":
            return job
        recipe = self.recipes[job["recipe"]]
        cancel = threading.Event()
        with self._lock:
            self._cancels[job_id] = cancel
            self.current_job_id = job_id
        self.store.update(job_id, lambda record: record.update(
            state="running", startedAt=now_iso(),
            owner={"pid": os.getpid(), "heartbeat": now_iso()},
        ))
        ctx = JobContext(self.store, job_id, self.presets_loader(), cancel)
        state, error = "done", None
        try:
            recipe.run(ctx)
        except StageCancelled:
            state = "cancelled"
        except Exception as exc:  # noqa: BLE001 - every failure must land in job.json
            state, error = "failed", str(exc) or exc.__class__.__name__
            ctx.log_line(traceback.format_exc())
        finally:
            ctx.flush_log()
            with self._lock:
                self._cancels.pop(job_id, None)
                self.current_job_id = None
        return self.store.update(job_id, lambda record: record.update(
            state=state, error=error, finishedAt=now_iso()))

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self.run_job(job_id)
            except JobNotFound:
                continue
            except Exception:  # noqa: BLE001 - keep the lane alive
                traceback.print_exc()
