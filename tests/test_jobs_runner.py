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
    runner = Runner(store, recipes={"fake": Recipe("fake", "fake", prepare, run)}, presets_loader=dict,
                    machine_lock_path=tmp_path / ".machine.lock")
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


def test_two_runners_serialize_and_waiting_job_can_be_cancelled(tmp_path):
    import fcntl
    from local_assets_engine.jobs import now_iso
    store, runner = make_runner(tmp_path, lambda ctx: pytest.fail("cancelled job ran"))
    job = runner.create("fake", {})
    with (store.root / ".engine.lock").open("a") as lane:
        fcntl.flock(lane, fcntl.LOCK_EX)
        worker = threading.Thread(target=runner.run_job, args=(job["id"],))
        worker.start()
        for _ in range(50):
            if store.load(job["id"]).get("owner"):
                break
            time.sleep(0.02)
        assert store.load(job["id"])["state"] == "queued"
        assert store.recover_interrupted() == 0
        runner.cancel(job["id"])
        worker.join(3)
        assert not worker.is_alive()
    assert store.load(job["id"])["state"] == "cancelled"


def test_distinct_runner_instances_never_enter_recipe_together(tmp_path):
    first_entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    def first(ctx):
        first_entered.set()
        release.wait(5)
    store, one = make_runner(tmp_path, first)
    _, two = make_runner(tmp_path, lambda ctx: second_entered.set())
    a, b = one.create("fake", {}), two.create("fake", {})
    ta = threading.Thread(target=one.run_job, args=(a["id"],))
    tb = threading.Thread(target=two.run_job, args=(b["id"],))
    ta.start()
    assert first_entered.wait(3)
    tb.start()
    try:
        assert not second_entered.wait(0.3)
    finally:
        release.set()
        ta.join(3)
        tb.join(3)
    assert second_entered.is_set()
    assert store.load(a["id"])["state"] == store.load(b["id"])["state"] == "done"


def test_external_cancellation_stops_a_silent_measured_process(tmp_path):
    entered = threading.Event()
    def run(ctx):
        with ctx.stage("quiet", "quiet") as stage:
            entered.set()
            stage.run([sys.executable, "-c", "import time; time.sleep(30)"])
    store, runner = make_runner(tmp_path, run)
    job = runner.create("fake", {})
    worker = threading.Thread(target=runner.run_job, args=(job["id"],))
    worker.start()
    assert entered.wait(3)
    other = Runner(JobStore(store.root), recipes=runner.recipes, presets_loader=dict)
    other.cancel(job["id"])
    worker.join(5)
    assert not worker.is_alive()
    assert store.load(job["id"])["state"] == "cancelled"


def test_queued_behind_live_job_survives_recovery_and_shutdown_cancels_it(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "HEARTBEAT_INTERVAL_S", 0.05)
    entered, release = threading.Event(), threading.Event()
    def run(ctx):
        entered.set()
        release.wait(5)
    store, runner = make_runner(tmp_path, run)
    runner.start()
    a = runner.submit("fake", {})
    assert entered.wait(3)
    b = runner.submit("fake", {})
    store.update(b["id"], lambda j: j["owner"].update(heartbeat="2020-01-01T00:00:00+09:00"))
    time.sleep(0.15)
    assert store.recover_interrupted() == 0
    runner.shutdown(timeout=0.01)
    release.set()
    runner._thread.join(3)
    assert store.load(b["id"])["state"] == "cancelled"


def test_job_updates_do_not_lose_writes_between_processes(tmp_path):
    import subprocess
    store = JobStore(tmp_path / "jobs")
    job = store.create("fake", {}, "counter")
    script = (
        "from pathlib import Path\nfrom local_assets_engine.jobs import JobStore\n"
        f"s=JobStore(Path({str(store.root)!r}))\n"
        f"for _ in range(30): s.update({job['id']!r}, lambda j: j.update(counter=j.get('counter', 0)+1))\n"
    )
    processes = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(3)]
    assert [p.wait(timeout=10) for p in processes] == [0, 0, 0]
    assert store.load(job["id"])["counter"] == 90


def test_initial_owner_is_published_atomically_before_other_engine_recovery(tmp_path, monkeypatch):
    store, runner = make_runner(tmp_path, lambda ctx: None)
    other = JobStore(store.root)
    create = store.create
    recovered = []
    def inspect_after_create(*args, **kwargs):
        job = create(*args, **kwargs)
        recovered.append(other.recover_interrupted())
        return job
    monkeypatch.setattr(store, "create", inspect_after_create)
    job = runner.submit("fake", {})
    assert recovered == [0] and job["state"] == "queued"
    assert runner.run_job(job["id"])["state"] == "done"


def test_presets_initialization_failure_closes_claim_and_cleans_runner(tmp_path):
    store, runner = make_runner(tmp_path, lambda ctx: None)
    job = runner.create("fake", {})
    def broken_presets():
        raise ValueError("invalid presets JSON")
    runner.presets_loader = broken_presets
    failed = runner.run_job(job["id"])
    assert failed["state"] == "failed" and "invalid presets JSON" in failed["error"]
    assert runner.current_job_id is None and runner._cancels == {}


def test_cancelled_attempt_records_process_time_and_memory(tmp_path):
    entered = threading.Event()
    def run(ctx):
        with ctx.stage("cancel", "cancel") as stage:
            entered.set()
            stage.run([sys.executable, "-c", "import time; time.sleep(30)"])
    store, runner = make_runner(tmp_path, run)
    job = runner.create("fake", {})
    worker = threading.Thread(target=runner.run_job, args=(job["id"],))
    worker.start()
    assert entered.wait(3)
    time.sleep(0.1)
    runner.cancel(job["id"])
    worker.join(5)
    attempt = store.load(job["id"])["stages"][0]["processes"][0]
    assert attempt["state"] == "cancelled" and attempt["seconds"] > 0
    assert attempt["peakMemoryBytes"] > 0 and attempt["measurementUnavailableReason"] is None


def test_download_evidence_survives_tail_rotation_and_progress_filter(tmp_path):
    def run(ctx):
        ctx.log_line("Downloading model: 42%|####")
        for index in range(100):
            ctx.log_line(f"subsequent log line {index}")
    store, runner = make_runner(tmp_path, run)
    job = runner.run_job(runner.create("fake", {})["id"])
    assert job["hadDownloads"] is True
    assert all("Downloading" not in line for line in job["logTail"])


def test_cached_fetching_is_not_marked_as_a_download(tmp_path):
    store, runner = make_runner(tmp_path, lambda ctx: ctx.log_line("Fetching 7 files: 100%|####"))
    job = runner.run_job(runner.create("fake", {})["id"])
    assert not job.get("hadDownloads", False)


def test_cached_file_progress_never_counts_as_a_generated_candidate(tmp_path):
    observed = []
    def run(ctx):
        with ctx.stage("generate", "generate") as stage:
            stage.run([sys.executable, "-c", "print('Fetching 7 files: 100%|###'); print('Sampling: 20%|##')"], units=2)
            observed.append(stage.fraction)
    store, runner = make_runner(tmp_path, run)
    job = runner.run_job(runner.create("fake", {})["id"])
    assert observed == [pytest.approx(0.1)]
    assert not job.get("hadDownloads", False)


def test_different_output_stores_share_injected_machine_lane(tmp_path):
    first, release, second = threading.Event(), threading.Event(), threading.Event()
    def run_first(ctx):
        first.set()
        release.wait(5)
    store_a, a = make_runner(tmp_path / "a", run_first)
    store_b, b = make_runner(tmp_path / "b", lambda ctx: second.set())
    a.machine_lock_path = b.machine_lock_path = tmp_path / "machine.lock"
    ja, jb = a.create("fake", {}), b.create("fake", {})
    ta = threading.Thread(target=a.run_job, args=(ja["id"],))
    tb = threading.Thread(target=b.run_job, args=(jb["id"],))
    ta.start()
    assert first.wait(3)
    tb.start()
    try:
        assert not second.wait(0.2)
    finally:
        release.set()
        ta.join(3)
        tb.join(3)
    assert second.is_set()
    assert store_a.load(ja["id"])["state"] == store_b.load(jb["id"])["state"] == "done"


def test_job_directory_symlink_never_reads_writes_or_serves_external_folder(tmp_path):
    import json
    store = JobStore(tmp_path / "jobs")
    job = store.create("fake", {}, "source")
    folder = store.job_dir(job["id"])
    external = tmp_path / "outside"
    folder.rename(external)
    (external / "secret.txt").write_text("secret")
    before = (external / "job.json").read_bytes()
    folder.symlink_to(external, target_is_directory=True)
    for operation in (lambda: store.job_dir(job["id"]), lambda: store.load(job["id"]),
                      lambda: store.update(job["id"], lambda j: j.update(title="changed")),
                      lambda: store.resolve_file(job["id"], "secret.txt")):
        with pytest.raises(JobNotFound):
            operation()
    assert store.list() == [] and (external / "job.json").read_bytes() == before


def test_record_symlink_and_mutated_id_cannot_redirect_update(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create("fake", {}, "source")
    with pytest.raises(JobNotFound):
        store.update(job["id"], lambda j: j.update(id="20261004-000000-abcd"))
    external = tmp_path / "external.json"
    external.write_text('{"secret":true}')
    record = store.job_dir(job["id"]) / "job.json"
    record.unlink()
    record.symlink_to(external)
    with pytest.raises(JobNotFound):
        store.load(job["id"])
    assert external.read_text() == '{"secret":true}'


def test_trash_reservation_blocks_new_sources_and_requires_its_token(tmp_path):
    from local_assets_engine.jobs import JobBusy
    store, runner = make_runner(tmp_path, lambda ctx: None)
    source = runner.run_job(runner.create("fake", {})["id"])
    reserved = store.reserve_trash(source["id"], "a" * 32)
    assert reserved["trashReservation"]["token"] == "a" * 32
    for params in ({"source": {"jobId": source["id"], "assetId": "a01"}},
                   {"assets": [{"source": {"jobId": source["id"], "assetId": "a01"}}]},
                   {"imagePath": str(store.job_dir(source["id"]) / "image.png")}):
        with pytest.raises(JobBusy):
            runner.create("fake", params)
    with pytest.raises(JobBusy):
        store.finish_trash(source["id"], "b" * 32)
    store.finish_trash(source["id"], "a" * 32, failed=True)
    assert "trashReservation" not in store.load(source["id"])
    assert runner.create("fake", {"source": {"jobId": source["id"], "assetId": "a01"}})["state"] == "queued"


def test_trash_reservations_only_expire_when_owner_process_dies(tmp_path):
    import os
    from local_assets_engine.jobs import JobBusy
    store, runner = make_runner(tmp_path, lambda ctx: None)
    source = runner.run_job(runner.create("fake", {})["id"])
    store.update(source["id"], lambda j: j.update(trashReservation={
        "token": "a" * 32, "ownerPid": os.getpid(), "createdAt": "2020-01-01T00:00:00+09:00"}))
    with pytest.raises(JobBusy):
        store.reserve_trash(source["id"], "b" * 32)
    store.update(source["id"], lambda j: j["trashReservation"].update(ownerPid=99_999_999))
    assert store.reserve_trash(source["id"], "b" * 32)["trashReservation"]["token"] == "b" * 32


def test_reediting_surviving_copy_does_not_require_its_deleted_ancestor(tmp_path):
    import shutil
    store = JobStore(tmp_path / "jobs")
    ancestor = store.create("fake", {}, "original")
    copy = store.create("fake", {}, "independent edited copy")
    old_reference = {"jobId": ancestor["id"], "assetId": "a01"}
    store.update(copy["id"], lambda j: j.update(state="done", assets=[{
        "id": "a01", "meta": {"source": old_reference}, "file": "copy.png"}]))
    (store.job_dir(copy["id"]) / "copy.png").write_bytes(b"own saved pixels")
    removed_path = str(store.job_dir(ancestor["id"]) / "source.png")
    shutil.rmtree(store.job_dir(ancestor["id"]))
    def prepare(params, _presets, _store):
        prior = store.load(params["source"]["jobId"])
        return {"source": params["source"], "inputPath": str(store.job_dir(copy["id"]) / "copy.png"),
                "sourceMeta": prior["assets"][0]["meta"],
                "plan": {"source": old_reference}, "subject": removed_path}, "new edit"
    recipe = Recipe("fake", "fake", prepare, lambda ctx: None)
    runner = Runner(store, recipes={"fake": recipe}, presets_loader=dict,
                    machine_lock_path=tmp_path / "machine.lock")
    job = runner.create("fake", {"source": {"jobId": copy["id"], "assetId": "a01"}})
    assert job["state"] == "queued"
    assert store.referenced_job_ids(job["params"]) == {copy["id"]}


def test_health_and_recovery_find_active_jobs_behind_newer_history(tmp_path, monkeypatch):
    import itertools
    import os
    from local_assets_engine import jobs as jobs_module
    from local_assets_engine.jobs import now_iso
    sequence = itertools.count()
    monkeypatch.setattr(jobs_module, "new_job_id", lambda: f"20261004-{next(sequence):06d}-abcd")
    store = JobStore(tmp_path / "jobs")
    oldest = store.create("fake", {}, "older running job")
    store.update(oldest["id"], lambda j: j.update(state="running", owner={"pid": os.getpid(), "heartbeat": now_iso()}))
    for _ in range(51):
        recent = store.create("fake", {}, "newer completed job")
        store.update(recent["id"], lambda j: j.update(state="done"))
    assert store.running_job_id() == oldest["id"]
    assert store.recover_interrupted() == 0
    store.update(oldest["id"], lambda j: j.update(owner={"pid": 99_999_999, "heartbeat": now_iso()}))
    assert store.recover_interrupted() == 1
    assert store.load(oldest["id"])["state"] == "failed"


def test_reserve_retry_is_idempotent_only_for_same_operation(tmp_path):
    from local_assets_engine.jobs import JobBusy
    store, runner = make_runner(tmp_path, lambda ctx: None)
    job = runner.run_job(runner.create("fake", {})["id"])
    validated = []
    token = "a" * 32
    first = store.reserve_trash(job["id"], token, validate=lambda j: validated.append(j["id"]), paths=["surface/a.npz"])
    second = store.reserve_trash(job["id"], token, validate=lambda j: pytest.fail("retry revalidated"), paths=["surface/a.npz"])
    assert second["trashReservation"] == first["trashReservation"]
    assert validated == [job["id"]]
    with pytest.raises(JobBusy):
        store.reserve_trash(job["id"], token, paths=["surface/b.npz"])


def test_finish_unknown_reserve_outcome_retires_token_before_late_request(tmp_path):
    from local_assets_engine.jobs import JobBusy
    import json
    store, runner = make_runner(tmp_path, lambda ctx: None)
    job = runner.run_job(runner.create("fake", {})["id"])
    token = "a" * 32
    store.finish_trash(job["id"], token, failed=True)
    assert json.loads((store.root / ".trash-finished" / token).read_text())["jobId"] == job["id"]
    with pytest.raises(JobBusy):
        store.reserve_trash(job["id"], token)
    assert not store.load(job["id"]).get("trashReservation")


def test_finished_token_cannot_release_newer_reservation_and_finish_can_retry(tmp_path):
    from local_assets_engine.jobs import JobBusy
    store, runner = make_runner(tmp_path, lambda ctx: None)
    job = runner.run_job(runner.create("fake", {})["id"])
    old, current, unknown = "a" * 32, "b" * 32, "c" * 32
    store.reserve_trash(job["id"], old)
    store.finish_trash(job["id"], old)
    store.finish_trash(job["id"], old)
    store.reserve_trash(job["id"], current)
    for token in (old, unknown):
        with pytest.raises(JobBusy):
            store.finish_trash(job["id"], token)
        assert store.load(job["id"])["trashReservation"]["token"] == current
        with pytest.raises(JobBusy):
            store.reserve_trash(job["id"], token)
    store.finish_trash(job["id"], current)
    store.finish_trash(job["id"], current)
    assert not store.load(job["id"]).get("trashReservation")


def test_finished_record_survives_whole_folder_removal_and_stale_owner(tmp_path):
    import shutil
    from local_assets_engine.jobs import JobBusy
    store, runner = make_runner(tmp_path, lambda ctx: None)
    job = runner.run_job(runner.create("fake", {})["id"])
    token = "a" * 32
    store.reserve_trash(job["id"], token)
    store.update(job["id"], lambda j: j["trashReservation"].update(ownerPid=99_999_999))
    store.assert_sources_available({"source": {"jobId": job["id"], "assetId": "a01"}})
    with pytest.raises(JobBusy):
        store.reserve_trash(job["id"], token)
    shutil.rmtree(store.job_dir(job["id"]))
    assert store.finish_trash(job["id"], token) is None
    assert store.finish_trash(job["id"], token) is None


def test_trash_token_path_and_tombstone_symlink_cannot_access_external_file(tmp_path):
    from local_assets_engine.jobs import JobBusy
    store, runner = make_runner(tmp_path, lambda ctx: None)
    job = runner.run_job(runner.create("fake", {})["id"])
    external = tmp_path / "outside.json"
    external.write_text('{"synthetic":"unchanged"}')
    for token in ("../outside.json", "x" * 32, "", True):
        with pytest.raises(JobBusy):
            store.reserve_trash(job["id"], token)
    folder = store.root / ".trash-finished"
    folder.mkdir(exist_ok=True)
    (folder / ("a" * 32)).symlink_to(external)
    with pytest.raises(JobBusy):
        store.finish_trash(job["id"], "a" * 32)
    assert external.read_text() == '{"synthetic":"unchanged"}'


def test_new_collector_records_explicit_false_and_preserves_existing_true(tmp_path):
    store, runner = make_runner(tmp_path, lambda ctx: None)
    fresh = runner.run_job(runner.create("fake", {})["id"])
    assert fresh["hadDownloads"] is False
    prior = runner.create("fake", {})
    store.update(prior["id"], lambda j: j.update(hadDownloads=True))
    assert runner.run_job(prior["id"])["hadDownloads"] is True
