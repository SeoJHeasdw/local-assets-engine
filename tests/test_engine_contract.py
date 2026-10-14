"""What arkAgent's engine contract adds to the job API: ``engine``·``contract`` in health,
``/api/recipes``, ``dir``·``findings``·``errorCode`` in job records, and the ``selftest`` recipe.
"""

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_assets_engine.bench import bench_rows
from local_assets_engine.jobs import ERROR_CODES, INTERRUPTED_ERROR, STOPPED_ERROR, JobStore, now_iso
from local_assets_engine.measure import StageFailed
from local_assets_engine.presets import PresetError
from local_assets_engine.recipes import RECIPES
from local_assets_engine.recipes.base import Recipe
from local_assets_engine.recipes.selftest import SELFTEST
from local_assets_engine.runner import Runner
from local_assets_engine.server import create_app


def run_recipe(tmp_path, run):
    store = JobStore(tmp_path / "jobs")
    runner = Runner(store, recipes={"fake": Recipe("fake", "fake", lambda p, _presets, _store: (p, "테스트"), run)},
                    presets_loader=dict, machine_lock_path=tmp_path / ".machine.lock")
    return store, runner.run_job(runner.create("fake", {})["id"])


def selftest_runner(tmp_path):
    store = JobStore(tmp_path / "jobs")
    return store, Runner(store, recipes={SELFTEST.id: SELFTEST}, presets_loader=dict,
                         machine_lock_path=tmp_path / ".machine.lock")


def test_a_job_record_names_its_folder_without_storing_the_path(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create("fake", {}, "t")
    assert (job["findings"], job["errorCode"]) == ([], None)
    assert job["dir"] == store.load(job["id"])["dir"] == str(store.job_dir(job["id"]))
    assert Path(job["dir"]).is_absolute()
    updated = store.update(job["id"], lambda record: record.update(title="again"))
    assert updated["dir"] == job["dir"]
    # 출력 폴더를 옮겨도 기록이 틀려지지 않게 절대 경로는 파일에 적지 않는다.
    assert "dir" not in json.loads((store.job_dir(job["id"]) / "job.json").read_text("utf-8"))


def test_records_written_before_the_contract_fields_still_read(tmp_path):
    store = JobStore(tmp_path / "jobs")
    expected = {}
    for state, error, code in (
        ("done", None, None),
        ("cancelled", "엔진이 다시 시작되어 대기 중이던 작업을 취소했습니다.", None),
        ("failed", INTERRUPTED_ERROR, "interrupted"),
        ("failed", "메모리가 부족해 macOS가 TRELLIS 프로세스를 종료했습니다. 3D 품질을 512로 낮추세요.", "out_of_memory"),
        ("failed", "영상 생성이 실패했습니다. (종료 코드 1: RuntimeError: MPS backend out of memory)", "out_of_memory"),
        ("failed", "이미지 파일이 만들어지지 않았습니다: seed-7.png", "engine_error"),
    ):
        job = store.create("image", {}, "old")
        path = store.job_dir(job["id"]) / "job.json"
        old = {key: value for key, value in json.loads(path.read_text("utf-8")).items()
               if key not in ("findings", "errorCode")}
        path.write_text(json.dumps({**old, "state": state, "error": error}, ensure_ascii=False), "utf-8")
        expected[job["id"]] = (code, path.read_bytes())
    for job in store.list():
        code, stored = expected[job["id"]]
        assert (job["findings"], job["errorCode"]) == ([], code)
        # 읽는 것만으로 과거 기록을 고쳐 쓰지 않는다.
        assert (store.job_dir(job["id"]) / "job.json").read_bytes() == stored
    assert len(expected) == 6


def test_failures_are_classified_by_their_cause(tmp_path):
    def exits(ctx):
        with ctx.stage("work", "작업") as stage:
            stage.run([sys.executable, "-c", "import sys; sys.exit('모델 파일이 깨졌습니다')"])

    def killed(ctx):
        with ctx.stage("work", "작업") as stage:
            try:
                stage.run([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"])
            except StageFailed as failure:
                # 3D·영상 레시피처럼 사람이 읽을 문장으로 감싸도 원인은 남는다.
                raise RuntimeError("메모리가 부족해 프로세스가 종료됐습니다.") from failure

    def allocator(ctx):
        with ctx.stage("work", "작업") as stage:
            stage.run([sys.executable, "-c",
                       "import sys; sys.exit('RuntimeError: MPS backend out of memory (MPS allocated: 30 GB)')"])

    def stale_request(_ctx):
        raise PresetError("알 수 없는 프리셋입니다: removed")

    for index, (run, code) in enumerate(((exits, "engine_error"), (killed, "out_of_memory"),
                                         (allocator, "out_of_memory"), (stale_request, "invalid_input"))):
        _store, job = run_recipe(tmp_path / str(index), run)
        assert (job["state"], job["errorCode"]) == ("failed", code), job["error"]
        assert job["error"] and job["errorCode"] in ERROR_CODES


def test_a_broken_engine_config_is_not_blamed_on_the_request(tmp_path):
    def broken_presets():
        raise PresetError("mesh: revision은 40자리 커밋 해시여야 합니다.")

    store = JobStore(tmp_path / "jobs")
    runner = Runner(store, recipes={"fake": Recipe("fake", "fake", lambda p, _presets, _store: (p, "테스트"),
                                                   lambda ctx: pytest.fail("must not run"))},
                    presets_loader=dict, machine_lock_path=tmp_path / ".machine.lock")
    job = runner.create("fake", {})
    # 대기하는 사이 설정 파일이 깨졌다. 요청은 그대로다.
    runner.presets_loader = broken_presets
    failed = runner.run_job(job["id"])
    assert (failed["state"], failed["errorCode"]) == ("failed", "engine_error")


def test_done_and_cancelled_jobs_have_no_error_code(tmp_path):
    _store, done = run_recipe(tmp_path / "done", lambda ctx: None)
    assert (done["state"], done["error"], done["errorCode"]) == ("done", None, None)
    store, runner = selftest_runner(tmp_path / "cancelled")
    job = runner.create("selftest", {"stepSeconds": 20})
    worker = threading.Thread(target=runner.run_job, args=(job["id"],))
    worker.start()
    deadline = time.monotonic() + 10
    while not store.load(job["id"])["stages"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runner.cancel(job["id"])["state"] == "cancelling"
    worker.join(10)
    cancelled = store.load(job["id"])
    assert (cancelled["state"], cancelled["error"], cancelled["errorCode"]) == ("cancelled", None, None)
    assert cancelled["stages"][0]["state"] == "cancelled" and not cancelled["assets"]


def test_a_restart_closes_running_and_queued_jobs_as_interrupted(tmp_path):
    store = JobStore(tmp_path / "jobs")
    running = store.create("fake", {}, "a")
    store.update(running["id"], lambda job: job.update(state="running"))
    queued = store.create("fake", {}, "b")
    assert store.recover_interrupted() == 2
    closed = store.load(running["id"])
    assert (closed["state"], closed["errorCode"], closed["error"]) == ("failed", "interrupted", INTERRUPTED_ERROR)
    # 사람이 취소하지 않았다. 다시 넣어야 할 작업으로 보이게 cancelled로 두지 않는다.
    never_ran = store.load(queued["id"])
    assert (never_ran["state"], never_ran["errorCode"]) == ("failed", "interrupted") and never_ran["error"]


def test_stopping_the_engine_interrupts_its_jobs_but_keeps_a_requested_cancel(tmp_path):
    store, runner = selftest_runner(tmp_path)
    runner.start()
    running = runner.submit("selftest", {"stepSeconds": 20})
    queued = runner.submit("selftest", {"stepSeconds": 20})
    deadline = time.monotonic() + 10
    while not store.load(running["id"])["stages"] and time.monotonic() < deadline:
        time.sleep(0.05)
    runner.shutdown()
    stopped, never_ran = store.load(running["id"]), store.load(queued["id"])
    assert (stopped["state"], stopped["errorCode"], stopped["error"]) == ("failed", "interrupted", STOPPED_ERROR)
    assert stopped["stages"][0]["processes"][0]["peakMemoryBytes"] > 0
    assert (never_ran["state"], never_ran["errorCode"]) == ("failed", "interrupted") and never_ran["error"]

    store, runner = selftest_runner(tmp_path / "requested")
    runner.start()
    job = runner.submit("selftest", {"stepSeconds": 20})
    while not store.load(job["id"])["stages"] and time.monotonic() < deadline + 10:
        time.sleep(0.05)
    runner.cancel(job["id"])
    runner.shutdown()
    assert (store.load(job["id"])["state"], store.load(job["id"])["errorCode"]) == ("cancelled", None)


def test_findings_point_at_evidence_and_leave_the_review_to_a_person(tmp_path):
    def run(ctx):
        output = ctx.dir / "final" / "a.png"
        output.parent.mkdir()
        output.write_bytes(b"png")
        asset = ctx.add_asset(kind="image", role="candidate", file=output)
        ctx.add_finding(severity="warn", code="image.touches_edge", params={"coverage": 0.4},
                        where="후보 1 · 시드 7", asset_id=asset["id"], evidence=[output])
        ctx.add_finding(severity="fail", code="image.subject_missing", where="후보 2 · 시드 8")
        with pytest.raises(ValueError):
            ctx.add_finding(severity="info", code="x", where="y")

    _store, job = run_recipe(tmp_path, run)
    assert job["state"] == "done", job["error"]
    assert job["findings"] == [
        {"id": "f01", "severity": "warn", "code": "image.touches_edge", "params": {"coverage": 0.4},
         "where": "후보 1 · 시드 7", "assetId": "a01", "evidence": ["final/a.png"], "review": "pending"},
        {"id": "f02", "severity": "fail", "code": "image.subject_missing", "params": {},
         "where": "후보 2 · 시드 8", "assetId": None, "evidence": [], "review": "pending"},
    ]
    assert job["assets"][0]["review"] == "pending"


def test_findings_written_before_their_review_field_read_as_pending(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create("image", {}, "old")
    path = store.job_dir(job["id"]) / "job.json"
    record = json.loads(path.read_text("utf-8"))
    record["findings"] = [{"id": "f01", "severity": "warn", "code": "image.touches_edge", "params": {},
                           "where": "후보 1", "assetId": None, "evidence": []}]
    path.write_text(json.dumps(record, ensure_ascii=False), "utf-8")
    assert store.load(job["id"])["findings"][0]["review"] == "pending"


def test_engine_start_discards_finished_selftest_jobs_only(tmp_path):
    store, runner = selftest_runner(tmp_path)
    finished = runner.run_job(runner.create("selftest", {"stepSeconds": 0})["id"])
    waiting = runner.create("selftest", {})
    other = store.create("image", {}, "kept")
    store.update(other["id"], lambda job: job.update(state="done"))
    # 다른 엔진이 대기열에 둔 점검 작업은 아직 끝나지 않았다. 지우지 않는다.
    store.update(waiting["id"], lambda job: job.update(owner={"pid": os.getpid(), "heartbeat": now_iso()}))
    runner.start()
    try:
        assert not store.job_dir(finished["id"]).exists()
        assert {job["id"] for job in store.list()} == {waiting["id"], other["id"]}
    finally:
        runner.shutdown()


def test_selftest_runs_measured_steps_and_leaves_an_asset_and_a_finding(tmp_path):
    store, runner = selftest_runner(tmp_path)
    created = runner.create("selftest", {"stepSeconds": 0})
    assert created["params"] == {"steps": 2, "stepSeconds": 0.0} and created["title"]
    job = runner.run_job(created["id"])
    assert (job["state"], job["errorCode"]) == ("done", None), job["error"]
    assert [stage["name"] for stage in job["stages"]] == ["step-1", "step-2"]
    for stage in job["stages"]:
        # 점검 단계도 생성 단계와 같은 측정 경로를 지난다.
        assert stage["state"] == "done" and stage["progress"] == 1.0
        assert stage["seconds"] >= 0 and stage["peakMemoryBytes"] > 0
        assert stage["processes"][0]["command"] == "local_assets_engine.tools.selftest_step"
    asset, finding = job["assets"][0], job["findings"][0]
    assert (asset["kind"], asset["review"]) == ("text", "pending")
    assert store.resolve_file(job["id"], asset["file"]).is_file()
    assert finding["assetId"] == asset["id"] and finding["evidence"] == [asset["file"]]
    assert finding["severity"] in ("fail", "warn") and finding["code"] and finding["where"]


def test_selftest_fails_at_the_requested_step(tmp_path):
    _store, runner = selftest_runner(tmp_path)
    job = runner.run_job(runner.create("selftest", {"steps": 3, "stepSeconds": 0, "failAt": 2})["id"])
    assert (job["state"], job["errorCode"]) == ("failed", "engine_error") and job["error"]
    assert [(stage["name"], stage["state"]) for stage in job["stages"]] == [("step-1", "done"), ("step-2", "failed")]
    assert not job["assets"] and not job["findings"]
    for params in ({"steps": 0}, {"steps": 11}, {"stepSeconds": -1}, {"stepSeconds": 31},
                   {"failAt": 0}, {"failAt": 3}, {"steps": True}, {"stepSeconds": "soon"}):
        with pytest.raises(PresetError):
            runner.create("selftest", params)


def test_finding_review_records_a_persons_check(engine):
    client, runner = engine
    job = runner.run_job(client.post("/api/jobs", json={"recipe": "selftest", "params": {"stepSeconds": 0}}).json()["id"])
    url = f"/api/jobs/{job['id']}/findings/{job['findings'][0]['id']}/review"
    cleared = client.post(url, json={"status": "cleared"}).json()["findings"][0]
    assert cleared["review"] == "cleared" and cleared["reviewedAt"]
    assert client.post(url, json={"status": "pending"}).json()["findings"][0]["review"] == "pending"
    assert client.post(url, json={"status": "approved"}).status_code == 400
    assert client.post(f"/api/jobs/{job['id']}/findings/f99/review", json={"status": "cleared"}).status_code == 404
    assert client.post("/api/jobs/20260101-000000-abcd/findings/f01/review", json={"status": "cleared"}).status_code == 404
    assert client.post(url, json={"status": "cleared"}, headers={"origin": "http://example.invalid"}).status_code == 403
    # 항목을 확인한 것은 에셋 승인과 따로다.
    assert runner.store.load(job["id"])["assets"][0]["review"] == "pending"


@pytest.fixture
def engine(tmp_path):
    store = JobStore(tmp_path / "jobs")
    runner = Runner(store, machine_lock_path=tmp_path / ".machine.lock")
    with TestClient(create_app(store=store, runner=runner, start_runner=False),
                    base_url="http://127.0.0.1:47831") as client:
        yield client, runner


def test_health_names_the_engine_and_the_contract(engine):
    client, _runner = engine
    health = client.get("/api/health").json()
    assert (health["ok"], health["engine"], health["contract"], health["currentJob"]) == (True, "assets", 0, None)
    assert health["busy"] is False and "busyBy" not in health
    assert isinstance(health["version"], str) and Path(health["outputDir"]).is_absolute()


def test_health_is_busy_while_a_job_runs_and_while_anyone_holds_the_lane(engine):
    client, runner = engine
    job = client.post("/api/jobs", json={"recipe": "selftest", "params": {"stepSeconds": 20}}).json()
    worker = threading.Thread(target=runner.run_job, args=(job["id"],))
    worker.start()
    deadline = time.monotonic() + 10
    while not runner.store.load(job["id"])["stages"] and time.monotonic() < deadline:
        time.sleep(0.05)
    health = client.get("/api/health").json()
    assert (health["busy"], health["currentJob"]) == (True, job["id"]) and "busyBy" not in health
    runner.cancel(job["id"])
    worker.join(10)
    assert client.get("/api/health").json()["busy"] is False
    # 대기열 밖의 점유: 다른 체크아웃의 엔진이나 제어 프로세스를 잃은 단계가 차선 잠금을 쥐고 있다.
    with runner.machine_lock_path.open("a") as lane:
        fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
        health = client.get("/api/health").json()
        assert (health["busy"], health["currentJob"], health["busyBy"]) == (True, None, "lane:.machine.lock")
    assert client.get("/api/health").json()["busy"] is False


def test_doctor_runs_in_its_own_process(engine, monkeypatch):
    from local_assets_engine import doctor
    client, _runner = engine
    calls = []

    def run(command, **options):
        calls.append(command)
        report = {"capabilities": {"image": True}, "checks": {}}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(report), stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    assert client.get("/api/doctor").json()["capabilities"] == {"image": True}
    assert client.get("/api/doctor", params={"deep": "true"}).status_code == 200
    assert calls[0][1:] == ["-m", "local_assets_engine", "doctor", "--json", "--quick"]
    assert calls[1][1:] == ["-m", "local_assets_engine", "doctor", "--json"]
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda command, **options: subprocess.CompletedProcess(command, 1, stdout="", stderr="boom"))
    failed = client.get("/api/doctor")
    assert failed.status_code == 500 and failed.json()["detail"]


def test_recipes_lists_what_the_runner_accepts(engine):
    client, runner = engine
    recipes = client.get("/api/recipes").json()["recipes"]
    assert [recipe["name"] for recipe in recipes] == list(runner.recipes) == list(RECIPES)
    for recipe in recipes:
        assert recipe["label"] and recipe["paramsSchema"]["type"] == "object"
        assert isinstance(recipe["needsReview"], bool)
    assert client.get("/api/recipes", headers={"origin": "http://example.invalid"}).status_code == 403


def test_selftest_jobs_stay_out_of_the_library_and_the_measurements(engine):
    client, runner = engine
    created = client.post("/api/jobs", json={"recipe": "selftest", "params": {"stepSeconds": 0}})
    assert created.status_code == 201 and created.json()["dir"]
    job = runner.run_job(created.json()["id"])
    assert job["state"] == "done" and job["assets"]
    # 작업 기록과 목록에는 그대로 보인다. 보관함과 장비 판단 근거에만 넣지 않는다.
    assert client.get("/api/jobs", params={"limit": 2}).json()["jobs"][0]["id"] == job["id"]
    assert client.get("/api/assets").json() == {"assets": []}
    assert bench_rows(runner.store) == []
    assert client.post("/api/jobs", json={"recipe": "selftest", "params": {"failAt": 9}}).status_code == 400
