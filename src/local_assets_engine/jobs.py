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
import stat
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ACTIVE_STATES = frozenset({"queued", "running", "cancelling"})
REVIEW_STATES = frozenset({"pending", "approved", "rejected"})
# 하트비트는 진행률을 쓸 때마다(≤0.5초) 갱신된다. 이보다 오래 멈춘 기록은 죽은 것으로 본다.
OWNER_STALE_AFTER_S = 180
_JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
_TRASH_TOKEN = re.compile(r"[0-9a-f]{32}")


def owner_alive(owner: dict[str, Any] | None) -> bool:
    """True while the process that started the job is still running it.

    앱과 CLI가 각자 엔진을 띄울 수 있다. 다른 프로세스가 돌리는 중인 작업을
    시작 복구가 닫아 버리면, 멀쩡히 돌던 생성이 남의 재시작에 끊긴다.
    """
    if not isinstance(owner, dict) or not _process_alive(owner.get("pid")):
        return False
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


class JobBusy(ValueError):
    """A generation/source or trash operation already owns this job."""


def _process_alive(pid: Any) -> bool:
    try:
        value = int(pid)
        if value <= 0:
            return False
        os.kill(value, 0)
        return True
    except (TypeError, ValueError, OverflowError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._namespace_lock = threading.RLock()
        self._namespace_local = threading.local()

    @contextmanager
    def namespace(self):
        """Serialize source claims/new jobs and trash reservations across engines."""
        with self._namespace_lock:
            if getattr(self._namespace_local, "depth", 0):
                self._namespace_local.depth += 1
                try:
                    yield
                finally:
                    self._namespace_local.depth -= 1
                return
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fd = None
            try:
                fd = os.open(".namespace.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
                             dir_fd=root_fd)
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._namespace_local.depth = 1
                yield
            finally:
                self._namespace_local.depth = 0
                if fd is not None:
                    os.close(fd)
                os.close(root_fd)

    def referenced_job_ids(self, params: Any) -> set[str]:
        """Actual recipe inputs, excluding lineage metadata, prompts and plans."""
        references: set[str] = set()
        if not isinstance(params, dict):
            return references

        def source(value: Any) -> None:
            if isinstance(value, dict) and _JOB_ID.fullmatch(str(value.get("jobId", ""))):
                references.add(value["jobId"])

        def file(value: Any) -> None:
            if not isinstance(value, str) or not Path(value).is_absolute():
                return
            candidates = [Path(value)]
            try:
                candidates.append(Path(value).resolve())
            except (ValueError, OSError, RuntimeError):
                pass
            for candidate in candidates:
                try:
                    relative = candidate.relative_to(self.root)
                    if relative.parts and _JOB_ID.fullmatch(relative.parts[0]):
                        references.add(relative.parts[0])
                except (ValueError, OSError, RuntimeError):
                    pass

        source(params.get("source"))
        for key in ("inputPath", "imagePath", "statePath", "rawPath", "previewPath", "path"):
            file(params.get(key))
        entries = params.get("assets")
        if isinstance(entries, list):
            for asset in entries:
                if isinstance(asset, dict):
                    source(asset.get("source"))
                    file(asset.get("file"))
                    file(asset.get("path"))
        stamps = params.get("stampPaths")
        if isinstance(stamps, dict):
            for path in stamps.values():
                file(path)
        return references

    def _clear_stale_reservation(self, job: dict[str, Any]) -> dict[str, Any]:
        reservation = job.get("trashReservation")
        if isinstance(reservation, dict) and not _process_alive(reservation.get("ownerPid")):
            token = reservation.get("token")
            if isinstance(token, str) and _TRASH_TOKEN.fullmatch(token):
                self._record_finished_trash(job["id"], token)
            return self.update(job["id"], lambda record: record.pop("trashReservation", None))
        return job

    @staticmethod
    def _validate_trash_token(token: str) -> None:
        if not isinstance(token, str) or not _TRASH_TOKEN.fullmatch(token):
            raise JobBusy("휴지통 이동 예약 값이 올바르지 않습니다.")

    @contextmanager
    def _trash_directory(self):
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory_fd = None
        try:
            try:
                os.mkdir(".trash-finished", mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            directory_fd = os.open(".trash-finished", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=root_fd)
            yield directory_fd
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            os.close(root_fd)

    def _finished_trash(self, token: str) -> dict[str, Any] | None:
        with self._trash_directory() as directory_fd:
            try:
                fd = os.open(token, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise JobBusy("휴지통 완료 기록 경로가 올바르지 않습니다.") from error
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise JobBusy("휴지통 완료 기록이 올바르지 않습니다.")
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = None
                    value = handle.read(4096)
                    if handle.read(1):
                        raise JobBusy("휴지통 완료 기록이 올바르지 않습니다.")
                record = json.loads(value)
                if not isinstance(record, dict) or not _JOB_ID.fullmatch(str(record.get("jobId", ""))):
                    raise JobBusy("휴지통 완료 기록이 올바르지 않습니다.")
                return record
            except (ValueError, OSError, RecursionError) as error:
                raise JobBusy("휴지통 완료 기록을 확인할 수 없습니다.") from error
            finally:
                if fd is not None:
                    os.close(fd)

    def _record_finished_trash(self, job_id: str, token: str) -> None:
        existing = self._finished_trash(token)
        if existing is not None:
            if existing["jobId"] != job_id:
                raise JobBusy("다른 작업의 휴지통 이동 예약입니다.")
            return
        with self._trash_directory() as directory_fd:
            temporary = "." + secrets.token_hex(16) + ".tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                         dir_fd=directory_fd)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"jobId": job_id, "finishedAt": now_iso()}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                # Exclusive, atomic publication: a crash cannot leave a partly
                # written tombstone, nor can a pre-existing symlink be followed.
                os.link(temporary, token, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                        follow_symlinks=False)
                os.fsync(directory_fd)
            finally:
                os.unlink(temporary, dir_fd=directory_fd)

    def assert_sources_available(self, params: Any) -> None:
        with self.namespace():
            for job_id in self.referenced_job_ids(params):
                job = self._clear_stale_reservation(self.load(job_id))
                if job.get("trashReservation"):
                    raise JobBusy("원본을 휴지통으로 옮기는 중입니다. 완료된 뒤 다시 시도하세요.")
                if job["state"] not in ACTIVE_STATES and self.process_in_use(job_id):
                    raise JobBusy("종료된 엔진의 단계 프로세스가 아직 원본을 사용합니다.")

    def process_in_use(self, job_id: str) -> bool:
        """Detect a surviving stage lease even after its controller was killed."""
        try:
            with self._directory(job_id) as directory_fd:
                try:
                    fd = os.open(".process.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory_fd)
                except FileNotFoundError:
                    return False
                except OSError:
                    return True  # An unverifiable lease must block destructive work.
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        return True
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return True
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    return False
                finally:
                    os.close(fd)
        except JobNotFound:
            return False

    def reserve_trash(self, job_id: str, token: str, validate=None, paths=None,
                      *, owner_pid: int | None = None) -> dict[str, Any]:
        self._validate_trash_token(token)
        with self.namespace():
            if self._finished_trash(token) is not None:
                raise JobBusy("이미 끝나거나 취소된 휴지통 이동 예약입니다.")
            job = self._clear_stale_reservation(self.load(job_id))
            if self._finished_trash(token) is not None:
                raise JobBusy("이미 끝나거나 취소된 휴지통 이동 예약입니다.")
            owner = os.getpid() if owner_pid is None else owner_pid
            if isinstance(owner, bool) or not isinstance(owner, int) or not _process_alive(owner):
                raise JobBusy("휴지통 이동을 맡은 프로세스가 종료됐습니다.")
            reservation = job.get("trashReservation")
            if (isinstance(reservation, dict) and reservation.get("token") == token
                    and reservation.get("ownerPid") == owner and reservation.get("paths") == paths):
                return job
            if job.get("trashReservation") or job["state"] in ACTIVE_STATES or self.process_in_use(job_id):
                raise JobBusy("진행 중이거나 이미 휴지통 이동을 예약한 작업입니다.")
            if validate is not None:
                validate(job)
            reservation = {"token": token, "createdAt": now_iso(), "ownerPid": owner, "paths": paths}
            return self.update(job_id, lambda record: record.update(trashReservation=reservation))

    def finish_trash(self, job_id: str, token: str, failed: bool = False) -> dict[str, Any] | None:
        self._validate_trash_token(token)
        with self.namespace():
            directory = self.job_dir(job_id)
            self._record_finished_trash(job_id, token)
            try:
                job = self.load(job_id)
            except JobNotFound:
                # A completed whole-job trash operation removed the directory.
                if not directory.exists():
                    return None
                raise
            reservation = job.get("trashReservation")
            if not reservation:
                # Also retires a reserve whose response/outcome was unknown.
                return job
            if reservation.get("token") != token:
                raise JobBusy("휴지통 이동 예약이 일치하지 않습니다.")
            return self.update(job_id, lambda record: record.pop("trashReservation", None))

    def job_dir(self, job_id: str) -> Path:
        if not _JOB_ID.fullmatch(str(job_id)):
            raise JobNotFound(job_id)
        path = self.root / job_id
        try:
            if path.is_symlink() or path.resolve().parent != self.root or self.root.resolve() != self.root:
                raise JobNotFound(job_id)
        except (OSError, RuntimeError) as error:
            raise JobNotFound(job_id) from error
        return path

    @contextmanager
    def _directory(self, job_id: str):
        self.job_dir(job_id)
        root_fd = directory_fd = None
        try:
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            directory_fd = os.open(job_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except OSError as error:
            if root_fd is not None:
                os.close(root_fd)
            raise JobNotFound(job_id) from error
        try:
            yield directory_fd
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            if root_fd is not None:
                os.close(root_fd)

    def create(self, recipe: str, params: dict[str, Any], title: str, *, owner=None) -> dict[str, Any]:
        with self.namespace(), self._lock:
            while True:
                job_id = new_job_id()
                try:
                    self.job_dir(job_id).mkdir()
                    break
                except FileExistsError:
                    continue
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
            if owner is not None:
                job["owner"] = dict(owner)
            self._write(job)
            return job

    def load(self, job_id: str) -> dict[str, Any]:
        with self._directory(job_id) as directory_fd:
            return self._load(job_id, directory_fd)

    def _load(self, job_id: str, directory_fd: int) -> dict[str, Any]:
        try:
            fd = os.open("job.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                job = json.load(handle)
        except OSError as error:
            raise JobNotFound(job_id) from error
        if not isinstance(job, dict) or job.get("id") != job_id:
            raise JobNotFound(job_id)
        return job

    def update(self, job_id: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        # API reviews/cancellation can update a CLI-owned job in another process.
        # Lock the read-modify-replace, not only the Python thread.
        with self._lock, self._directory(job_id) as directory_fd:
            try:
                lock_fd = os.open(".record.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
                                  dir_fd=directory_fd)
            except OSError as error:
                raise JobNotFound(job_id) from error
            with os.fdopen(lock_fd, "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                job = self._load(job_id, directory_fd)
                mutate(job)
                if job.get("id") != job_id:
                    raise JobNotFound(job_id)
                self._write(job, directory_fd=directory_fd)
                return job

    def list(self, limit: int | None = 200) -> list[dict[str, Any]]:
        jobs = []
        for job in self._records():
            if limit is not None and len(jobs) >= limit:
                break
            jobs.append(job)
        return jobs

    def _records(self):
        """Stream records so lifecycle scans neither truncate nor retain all JSON."""
        for entry in sorted(self.root.iterdir(), key=lambda p: p.name, reverse=True):
            if not entry.is_symlink() and entry.is_dir() and _JOB_ID.fullmatch(entry.name) and (entry / "job.json").exists():
                try:
                    yield self.load(entry.name)
                except (json.JSONDecodeError, JobNotFound):
                    continue

    def recover_interrupted(self) -> int:
        """Close jobs a stopped engine left open. Nothing restarts on its own."""
        # 대기 작업을 저절로 다시 돌리면 사용자가 모르는 사이 GPU를 오래 점유한다.
        closed = 0
        for job in self._records():
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

            try:
                self.update(job["id"], close)
            except JobNotFound:
                # A concurrently cancelled job may already have reached trash.
                continue
        return closed

    def running_job_id(self) -> str | None:
        """The job a live process is working on, whichever engine started it."""
        for job in self._records():
            if job["state"] in ("running", "cancelling") and owner_alive(job.get("owner")):
                return job["id"]
        return None

    def resolve_file(self, job_id: str, relative: str) -> Path:
        base = self.job_dir(job_id).resolve()
        target = (base / relative).resolve()
        if base not in target.parents or not target.is_file():
            raise JobNotFound(f"{job_id}/{relative}")
        return target

    def _write(self, job: dict[str, Any], *, directory_fd: int | None = None) -> None:
        if directory_fd is None:
            with self._directory(job["id"]) as fd:
                self._write(job, directory_fd=fd)
            return
        temporary = ".job-" + secrets.token_hex(8) + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=directory_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(job, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, "job.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
