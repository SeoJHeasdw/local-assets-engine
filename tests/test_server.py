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


def test_local_image_upload_and_source_validation(api):
    import io
    from PIL import Image
    client, _runner = api
    image=io.BytesIO();Image.new('RGBA',(8,8),'red').save(image,format='PNG')
    result=client.post('/api/uploads',content=image.getvalue(),headers={'content-type':'image/png'})
    assert result.status_code==201
    upload=result.json();assert upload['width']==8 and client.get(upload['url']).status_code==200
    assert client.post('/api/uploads',content=b'not an image').status_code==400
    assert client.post('/api/uploads',content=image.getvalue(),headers={'origin':'https://evil.example'}).status_code==403
    assert client.get('/uploads/invalid').status_code==404


def test_assets_are_organized_with_favorites_tags_collections_and_notes(api):
    client, runner = api
    job_id = client.post("/api/jobs", json={"recipe": "fake", "params": {"subject": "검"}}).json()["id"]
    runner.run_job(job_id)
    url = f"/api/jobs/{job_id}/assets/a01/library"
    updated = client.post(url, json={"favorite": True, "tags": [" 무기 ", "무기", "UI"], "collection": " 던전  1장 ", "note": "칼날이 선명함"}).json()
    asset = updated["assets"][0]
    assert (asset["favorite"], asset["tags"], asset["collection"], asset["note"]) == (True, ["무기", "UI"], "던전 1장", "칼날이 선명함")
    assert asset["review"] == "pending"
    assert [a["id"] for a in client.get("/api/assets", params={"favorite": "true", "collection": "던전 1장", "tag": "UI"}).json()["assets"]] == ["a01"]
    assert client.get("/api/assets", params={"favorite": "false"}).json()["assets"] == []
    cleared = client.post(url, json={"favorite": False, "tags": [], "collection": ""}).json()["assets"][0]
    assert "favorite" not in cleared and "tags" not in cleared and "collection" not in cleared
    assert client.post(url, json={"favorite": "yes"}).status_code == 400
    assert client.post(url, json={"tags": [f"t{i}" for i in range(21)]}).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/assets/a99/library", json={"favorite": True}).status_code == 404


def test_version_lineage_and_storage_classification(api, tmp_path):
    client, runner = api
    store = runner.store
    root = store.create("fake", {"subject": "상자"}, "상자")
    directory = store.job_dir(root["id"])
    (directory / "mesh/surface").mkdir(parents=True)
    for name, size in (("mesh/asset.glb", 10), ("mesh/source.npz", 20), ("mesh/input.png", 3), ("mesh/surface/full.npz", 40), ("mesh/asset.opt.glb", 5)):
        (directory / name).write_bytes(b"x" * size)
    store.update(root["id"], lambda job: job.update(state="done", assets=[{"id": "a01", "kind": "mesh", "role": "final", "file": "mesh/asset.glb", "review": "pending",
        "meta": {"sourceStateFile": "mesh/source.npz", "optimizedFile": "mesh/asset.opt.glb"}}]))
    edit = store.create("edit-asset", {"subject": "파란 상자"}, "파란 상자 · 편집본")
    (store.job_dir(edit["id"]) / "edit").mkdir()
    (store.job_dir(edit["id"]) / "edit/asset.glb").write_bytes(b"y" * 7)
    store.update(edit["id"], lambda job: job.update(state="done", assets=[{"id": "a01", "kind": "mesh", "role": "final", "file": "edit/asset.glb", "review": "pending",
        "createdAt": "2026-09-17T01:00:00+09:00", "meta": {"source": {"jobId": root["id"], "assetId": "a01"},
        "editPlan": {"layers": [{"type": "color"}, {"type": "stamp", "text": "JAVIS"}]}}}]))
    versions = client.get(f"/api/jobs/{edit['id']}/assets/a01/versions").json()
    assert versions["root"] == f"{root['id']}/a01"
    assert [(v["depth"], v["relation"], v["summary"]) for v in versions["versions"]] == [(0, None, []), (1, "편집", ["색 1", "문구 JAVIS"])]
    assert client.get(f"/api/jobs/{edit['id']}/assets/a09/versions").status_code == 404
    storage = client.get(f"/api/jobs/{root['id']}/storage").json()
    categories = {item["path"]: item["category"] for item in storage["files"]}
    assert categories["mesh/asset.glb"] == "results" and categories["mesh/source.npz"] == "bases"
    assert categories["mesh/input.png"] == "bases" and categories["mesh/surface/full.npz"] == "intermediate"
    assert categories["mesh/asset.opt.glb"] == "copies" and categories["job.json"] == "records" and storage["active"] is False
    report = client.get("/api/storage").json()
    row = next(item for item in report["jobs"] if item["jobId"] == root["id"])
    assert row["intermediate"] == ["mesh/surface/full.npz"] and row["usedBy"] == [edit["id"]]
    assert report["categories"]["intermediate"] == 40
