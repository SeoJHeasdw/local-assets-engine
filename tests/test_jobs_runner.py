import sys
import threading
import time

import pytest

from local_assets_engine.jobs import JobNotFound, JobStore
from local_assets_engine.presets import PresetError
from local_assets_engine.recipes.base import Recipe
from local_assets_engine import runner as runner_module
from local_assets_engine.runner import Runner, UnitTracker


def make_runner(tmp_path, run):
    def prepare(params, _presets, _store):
        if params.get("bad"):
            raise PresetError("bad params")
        return {"value": params.get("value", 1)}, "테스트 작업"

    store = JobStore(tmp_path / "jobs")
    runner = Runner(store, recipes={"fake": Recipe("fake", "fake", prepare, run)}, presets_loader=dict)
    return store, runner


def test_unit_tracker_counts_restarting_bars():
    tracker = UnitTracker(2)
    assert tracker.update(0.5) == 0.25
    assert tracker.update(1.0) == 0.5
    assert tracker.update(0.1) == pytest.approx(0.55)
    assert tracker.update(1.0) == 1.0


def test_successful_job_records_measurement_log_and_assets(tmp_path):
    def run(ctx):
        with ctx.stage("work", "작업") as stage:
            stage.run([sys.executable, "-c", "print('@@progress {\"done\": 1, \"total\": 1}'); print('line')"])
            output = ctx.dir / "result.txt"
            output.write_text("ok")
        ctx.add_asset(kind="image", role="candidate", file=output, meta={"n": 1})

    store, runner = make_runner(tmp_path, run)
    finished = runner.run_job(runner.create("fake", {"value": 3})["id"])
    assert finished["state"] == "done" and finished["error"] is None
    stage = finished["stages"][0]
    assert stage["state"] == "done" and stage["progress"] == 1.0
    assert stage["processes"][0]["peakMemoryBytes"] > 0
    assert stage["peakMemoryBytes"] == stage["processes"][0]["peakMemoryBytes"]
    asset = finished["assets"][0]
    assert (asset["id"], asset["file"], asset["review"], asset["meta"]) == ("a01", "result.txt", "pending", {"n": 1})
    assert "line" in finished["logTail"]


def test_failed_stage_marks_job_failed_with_reason(tmp_path):
    def run(ctx):
        with ctx.stage("work", "작업") as stage:
            stage.run([sys.executable, "-c", "import sys; sys.exit('모델 없음')"])

    _store, runner = make_runner(tmp_path, run)
    finished = runner.run_job(runner.create("fake", {})["id"])
    assert finished["state"] == "failed" and "모델 없음" in finished["error"]
    assert finished["stages"][0]["state"] == "failed"


def test_cancel_running_job_stops_its_process(tmp_path):
    started = threading.Event()

    def run(ctx):
        with ctx.stage("work", "작업") as stage:
            started.set()
            stage.run([sys.executable, "-c", "import time; time.sleep(30)"])

    store, runner = make_runner(tmp_path, run)
    job = runner.create("fake", {})
    worker = threading.Thread(target=runner.run_job, args=(job["id"],))
    worker.start()
    assert started.wait(5)
    time.sleep(0.3)
    assert runner.cancel(job["id"])["state"] == "cancelling"
    worker.join(10)
    finished = store.load(job["id"])
    assert finished["state"] == "cancelled"
    assert finished["stages"][0]["state"] == "cancelled"


def flaky_script(tmp_path, stderr_line, *, fail_always=False):
    marker = tmp_path / "attempted"
    script = tmp_path / "flaky.py"
    script.write_text(
        "import pathlib, sys\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        f"if marker.exists() and not {fail_always}:\n"
        "    print('ok')\n"
        "else:\n"
        "    marker.write_text('1')\n"
        f"    print({stderr_line!r}, file=sys.stderr)\n"
        "    sys.exit(1)\n",
        "utf-8",
    )
    return script


def test_a_busy_gpu_failure_is_retried_once(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "RETRY_PAUSE_S", 0.0)
    script = flaky_script(
        tmp_path,
        "[METAL] Command buffer execution failed: Caused GPU Timeout Error "
        "(00000002:kIOGPUCommandBufferCallbackErrorTimeout).",
    )

    def run(ctx):
        with ctx.stage("work", "작업") as stage:
            stage.run([sys.executable, str(script)], retries=1)

    _store, runner = make_runner(tmp_path, run)
    finished = runner.run_job(runner.create("fake", {})["id"])
    assert finished["state"] == "done"
    # 실패한 시도의 측정도 남는다: 프로세스 두 번.
    assert len(finished["stages"][0]["processes"]) == 2


def test_other_failures_are_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "RETRY_PAUSE_S", 0.0)
    script = flaky_script(tmp_path, "모델 파일이 깨졌습니다")

    def run(ctx):
        with ctx.stage("work", "작업") as stage:
            stage.run([sys.executable, str(script)], retries=1)

    _store, runner = make_runner(tmp_path, run)
    finished = runner.run_job(runner.create("fake", {})["id"])
    assert finished["state"] == "failed"
    assert len(finished["stages"][0]["processes"]) == 1


def test_cancelled_queued_job_never_runs(tmp_path):
    _store, runner = make_runner(tmp_path, lambda ctx: pytest.fail("must not run"))
    job = runner.create("fake", {})
    assert runner.cancel(job["id"])["state"] == "cancelled"
    assert runner.run_job(job["id"])["state"] == "cancelled"


def test_invalid_requests_create_no_job(tmp_path):
    store, runner = make_runner(tmp_path, lambda ctx: None)
    with pytest.raises(PresetError):
        runner.create("fake", {"bad": True})
    with pytest.raises(PresetError):
        runner.create("missing", {})
    assert store.list() == []


def test_recover_interrupted_closes_open_jobs_without_restarting(tmp_path):
    store = JobStore(tmp_path)
    running = store.create("fake", {}, "a")
    store.update(running["id"], lambda job: job.update(state="running"))
    queued = store.create("fake", {}, "b")
    done = store.create("fake", {}, "c")
    store.update(done["id"], lambda job: job.update(state="done"))
    assert store.recover_interrupted() == 2
    assert store.load(running["id"])["state"] == "failed"
    assert store.load(queued["id"])["state"] == "cancelled"
    assert store.load(done["id"])["state"] == "done"


def test_recover_interrupted_leaves_a_job_another_process_is_running(tmp_path):
    import os

    from local_assets_engine.jobs import now_iso

    store = JobStore(tmp_path)
    mine = store.create("fake", {}, "live")
    store.update(mine["id"], lambda job: job.update(
        state="running", owner={"pid": os.getpid(), "heartbeat": now_iso()}))
    stale = store.create("fake", {}, "stale")
    store.update(stale["id"], lambda job: job.update(
        state="running", owner={"pid": os.getpid(), "heartbeat": "2020-01-01T00:00:00+09:00"}))
    dead = store.create("fake", {}, "dead")
    store.update(dead["id"], lambda job: job.update(
        state="running", owner={"pid": 99_999_999, "heartbeat": now_iso()}))

    assert store.recover_interrupted() == 2
    assert store.load(mine["id"])["state"] == "running"
    assert store.load(stale["id"])["state"] == "failed"
    assert store.load(dead["id"])["state"] == "failed"


def test_resolve_file_blocks_traversal(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create("fake", {}, "t")
    (store.job_dir(job["id"]) / "ok.png").write_bytes(b"x")
    (tmp_path / "secret.txt").write_text("s")
    assert store.resolve_file(job["id"], "ok.png").name == "ok.png"
    with pytest.raises(JobNotFound):
        store.resolve_file(job["id"], "../../secret.txt")
    with pytest.raises(JobNotFound):
        store.job_dir("../etc")


def test_a_silent_stage_keeps_the_job_alive(tmp_path, monkeypatch):
    # 출력이 없는 동안에도 하트비트가 갱신돼야 다른 엔진이 이 작업을 닫지 않는다.
    monkeypatch.setattr(runner_module, "HEARTBEAT_INTERVAL_S", 0.05)
    beats = []

    def run(ctx):
        with ctx.stage("quiet", "조용한 단계"):
            for _ in range(20):
                beats.append(ctx.store.load(ctx.job_id)["owner"]["heartbeat"])
                time.sleep(0.05)

    store, runner = make_runner(tmp_path, run)
    job = runner.run_job(runner.create("fake", {})["id"])
    assert job["state"] == "done"
    assert len(set(beats)) > 1, "조용한 단계에서 하트비트가 멈췄다"


def test_a_running_job_survives_another_engine_starting(tmp_path):
    started, release = threading.Event(), threading.Event()

    def run(ctx):
        with ctx.stage("quiet", "조용한 단계"):
            started.set()
            release.wait(5)

    store, runner = make_runner(tmp_path, run)
    job = runner.create("fake", {})
    worker = threading.Thread(target=runner.run_job, args=(job["id"],))
    worker.start()
    assert started.wait(5)
    assert store.recover_interrupted() == 0
    assert store.load(job["id"])["state"] == "running"
    release.set()
    worker.join(10)
    assert store.load(job["id"])["state"] == "done"
