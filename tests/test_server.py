import os

import pytest
from fastapi.testclient import TestClient

from local_assets_engine.jobs import JobStore, now_iso
from local_assets_engine.presets import PresetError
from local_assets_engine.recipes.base import Recipe
from local_assets_engine.runner import Runner
from local_assets_engine.server import create_app


@pytest.fixture
def api(tmp_path):
    def prepare(params, _presets, _store):
        if not params.get("subject"):
            raise PresetError("무엇을 만들지 적어 주세요.")
        return {"subject": params["subject"]}, params["subject"]

    def run(ctx):
        output = ctx.dir / "a.png"
        output.write_bytes(b"png")
        ctx.add_asset(kind="image", role="candidate", file=output)

    store = JobStore(tmp_path / "jobs")
    runner = Runner(store, recipes={"fake": Recipe("fake", "fake", prepare, run)}, presets_loader=dict)
    app = create_app(store=store, runner=runner, start_runner=False)
    with TestClient(app, base_url="http://127.0.0.1:47831") as client:
        yield client, runner


def test_health_and_non_local_requests_are_rejected(api):
    client, _runner = api
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 403
    response = client.post(
        "/api/jobs", json={"recipe": "fake", "params": {"subject": "x"}},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_health_reports_a_job_another_process_is_running(api):
    client, runner = api
    job = runner.store.create("fake", {"subject": "x"}, "x")
    runner.store.update(job["id"], lambda record: record.update(
        state="running", owner={"pid": os.getpid(), "heartbeat": now_iso()}))
    assert client.get("/api/health").json()["currentJob"] == job["id"]


def test_job_lifecycle_review_listing_and_files(api):
    client, runner = api
    response = client.post("/api/jobs", json={"recipe": "fake", "params": {"subject": "검"}})
    assert response.status_code == 201
    job_id = response.json()["id"]
    runner.run_job(job_id)
    assert client.get(f"/api/jobs/{job_id}").json()["state"] == "done"
    reviewed = client.post(f"/api/jobs/{job_id}/assets/a01/review", json={"status": "approved"}).json()
    assert reviewed["assets"][0]["review"] == "approved"
    assets = client.get("/api/assets", params={"review": "approved"}).json()["assets"]
    assert [(asset["jobId"], asset["id"]) for asset in assets] == [(job_id, "a01")]
    assert client.get(f"/files/{job_id}/a.png").content == b"png"
    assert client.get(f"/files/{job_id}/..%2F..%2Fsecret").status_code == 404


def test_the_screen_is_never_served_from_the_app_cache(api):
    client, runner = api
    # 앱 창이 예전 화면을 캐시에서 꺼내 쓰면 고친 화면이 나오지 않는다.
    assert client.get("/api/presets").headers["cache-control"] == "no-store"
    job_id = client.post("/api/jobs", json={"recipe": "fake", "params": {"subject": "검"}}).json()["id"]
    runner.run_job(job_id)
    # 작업 결과 파일은 한번 만들어지면 바뀌지 않으므로 캐시를 막지 않는다.
    assert "cache-control" not in client.get(f"/files/{job_id}/a.png").headers


def test_invalid_requests_return_clear_errors(api):
    client, _runner = api
    assert client.post("/api/jobs", json={"recipe": "fake", "params": {}}).status_code == 400
    assert client.post("/api/jobs", json={"recipe": "nope"}).status_code == 400
    assert client.get("/api/jobs/20260101-000000-abcd").status_code == 404
    assert client.post("/api/jobs/bad-id/cancel").status_code == 404
    bad_review = client.post("/api/jobs/20260101-000000-abcd/assets/a01/review", json={"status": "maybe"})
    assert bad_review.status_code == 400
