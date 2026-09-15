"""Jobs on disk: one folder per job, ``job.json`` is the single record.

The runner thread and API requests both write the same record, so every write
goes through :meth:`JobStore.update`, which re-reads under one lock.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ACTIVE_STATES = frozenset({"queued", "running", "cancelling"})
REVIEW_STATES = frozenset({"pending", "approved", "rejected"})
_JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")


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
        with self._lock:
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
            if job["state"] not in ACTIVE_STATES:
                continue

            def close(record: dict[str, Any], was_running: bool = job["state"] != "queued") -> None:
                record["state"] = "failed" if was_running else "cancelled"
                record["error"] = ("엔진이 다시 시작되어 작업이 중단됐습니다." if was_running
                                   else "엔진이 다시 시작되어 대기 중이던 작업을 취소했습니다.")
                record["finishedAt"] = now_iso()
                for stage in record["stages"]:
                    if stage.get("state") == "running":
                        stage["state"] = "failed"

            self.update(job["id"], close)
            closed += 1
        return closed

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
