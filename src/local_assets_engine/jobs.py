"""Jobs on disk: one folder per job, ``job.json`` is the single record.

The runner thread and API requests both write the same record, so every write
goes through :meth:`JobStore.update`, which re-reads under one lock.
"""

from __future__ import annotations

import json
import fcntl
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ACTIVE_STATES = frozenset({"queued", "running", "cancelling"})
REVIEW_STATES = frozenset({"pending", "approved", "rejected"})
# 하트비트는 진행률을 쓸 때마다(≤0.5초) 갱신된다. 이보다 오래 멈춘 기록은 죽은 것으로 본다.
OWNER_STALE_AFTER_S = 180
_JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")


def owner_alive(owner: dict[str, Any] | None) -> bool:
    """True while the process that started the job is still running it.

    앱과 CLI가 각자 엔진을 띄울 수 있다. 다른 프로세스가 돌리는 중인 작업을
    시작 복구가 닫아 버리면, 멀쩡히 돌던 생성이 남의 재시작에 끊긴다.
    """
    if not owner:
        return False
    try:
        os.kill(int(owner["pid"]), 0)
    except (KeyError, TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    heartbeat = owner.get("heartbeat")
    if not heartbeat:
        return False
    try:
        age = (datetime.now().astimezone() - datetime.fromisoformat(heartbeat)).total_seconds()
    except (TypeError, ValueError):
        return False
    return age < OWNER_STALE_AFTER_S


class JobNotFound(KeyError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def job_dir(self, job_id: str) -> Path:
        if not _JOB_ID.match(str(job_id)):
            raise JobNotFound(job_id)
        return self.root / job_id

    def create(self, recipe: str, params: dict[str, Any], title: str) -> dict[str, Any]:
        with self._lock:
            job_id = new_job_id()
            while self.job_dir(job_id).exists():
                job_id = new_job_id()
            self.job_dir(job_id).mkdir(parents=True)
            job = {
                "id": job_id,
                "recipe": recipe,
                "title": title,
                "params": params,
                "state": "queued",
                "createdAt": now_iso(),
                "startedAt": None,
                "finishedAt": None,
                "stages": [],
                "assets": [],
                "error": None,
                "logTail": [],
            }
            self._write(job)
            return job

    def load(self, job_id: str) -> dict[str, Any]:
        path = self.job_dir(job_id) / "job.json"
        try:
            return json.loads(path.read_text("utf-8"))
        except FileNotFoundError as error:
            raise JobNotFound(job_id) from error

    def update(self, job_id: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        # API reviews/cancellation can update a CLI-owned job in another process.
        # Lock the read-modify-replace, not only the Python thread.
        directory = self.job_dir(job_id)
        if not directory.is_dir():
            raise JobNotFound(job_id)
        with self._lock, (directory / ".record.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            job = self.load(job_id)
            mutate(job)
            self._write(job)
            return job

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        jobs = []
        for entry in sorted(self.root.iterdir(), key=lambda p: p.name, reverse=True):
            if len(jobs) >= limit:
                break
            if entry.is_dir() and _JOB_ID.match(entry.name) and (entry / "job.json").exists():
                try:
                    jobs.append(self.load(entry.name))
                except (json.JSONDecodeError, JobNotFound):
                    continue
        return jobs

    def recover_interrupted(self) -> int:
        """Close jobs a stopped engine left open. Nothing restarts on its own."""
        # 대기 작업을 저절로 다시 돌리면 사용자가 모르는 사이 GPU를 오래 점유한다.
        closed = 0
        for job in self.list(limit=10_000):
            if job["state"] not in ACTIVE_STATES or owner_alive(job.get("owner")):
                continue

            def close(record: dict[str, Any]) -> None:
                nonlocal closed
                # Recheck under the process lock: a waiting runner may have
                # claimed this record since the directory snapshot was read.
                if record["state"] not in ACTIVE_STATES or owner_alive(record.get("owner")):
                    return
                was_running = record["state"] != "queued"
                record["state"] = "failed" if was_running else "cancelled"
                record["error"] = ("엔진이 다시 시작되어 작업이 중단됐습니다." if was_running
                                   else "엔진이 다시 시작되어 대기 중이던 작업을 취소했습니다.")
                record["finishedAt"] = now_iso()
                for stage in record["stages"]:
                    if stage.get("state") == "running":
                        stage["state"] = "failed"
                closed += 1

            self.update(job["id"], close)
        return closed

    def running_job_id(self) -> str | None:
        """The job a live process is working on, whichever engine started it."""
        for job in self.list(limit=50):
            if job["state"] in ("running", "cancelling") and owner_alive(job.get("owner")):
                return job["id"]
        return None

    def resolve_file(self, job_id: str, relative: str) -> Path:
        base = self.job_dir(job_id).resolve()
        target = (base / relative).resolve()
        if base not in target.parents or not target.is_file():
            raise JobNotFound(f"{job_id}/{relative}")
        return target

    def _write(self, job: dict[str, Any]) -> None:
        path = self.job_dir(job["id"]) / "job.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), "utf-8")
        os.replace(temporary, path)
