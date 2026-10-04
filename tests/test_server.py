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


@pytest.mark.parametrize("origin", ["null", "http://127.0.0.1:9999", "https://127.0.0.1:47831",
                                    "http://127.0.0.1:47831/", "http://localhost:47831", "not-a-url"])
def test_only_the_exact_studio_origin_can_change_state(api, origin):
    client, runner = api
    job = runner.submit("fake", {"subject": "x"})
    assert client.post(f"/api/jobs/{job['id']}/cancel", headers={"origin": origin}).status_code == 403
    assert runner.store.load(job["id"])["state"] == "queued"
    assert client.post("/api/uploads", content=b"x", headers={"origin": origin}).status_code == 403
    assert client.get("/api/health", headers={"origin": "http://127.0.0.1:47831"}).status_code == 200
    assert client.get("/api/health", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.get("/api/health", headers={"sec-fetch-site": "none"}).status_code == 200


def test_malformed_request_values_are_user_errors(tmp_path):
    store = JobStore(tmp_path / "jobs")
    runner = Runner(store)
    with TestClient(create_app(store=store, runner=runner, start_runner=False),
                    base_url="http://127.0.0.1:47831", raise_server_exceptions=False) as client:
        for params in ([1], "wrong", False, None):
            assert client.post("/api/jobs", json={"recipe": "image", "params": params}).status_code == 400
        for source in ([1], "wrong", 1, True):
            for recipe in ("image-to-3d", "image-to-video"):
                assert client.post("/api/jobs", json={"recipe": recipe, "params": {"source": source}}).status_code == 400
        assert client.post("/api/jobs", content='{"recipe":"image","params":{"subject":"x","width":1e400}}',
                           headers={"content-type": "application/json"}).status_code == 400
        for status in ({}, ["approved"], None):
            assert client.post("/api/jobs/20261004-000000-abcd/assets/a01/review", json={"status": status}).status_code == 400
        assert client.post("/api/jobs", json={"recipe": "edit-asset", "params": {
            "source": {"jobId": "20261004-000000-abcd", "assetId": "a01"}, "plan": {"layers": []}}}).status_code == 400


def real_source(store):
    from PIL import Image
    job = store.create("image", {"subject": "synthetic"}, "source")
    Image.new("RGBA", (8, 8), "red").save(store.job_dir(job["id"]) / "image.png")
    return store.update(job["id"], lambda j: j.update(state="done", assets=[{
        "id": "a01", "kind": "image", "role": "candidate", "file": "image.png",
        "preview": "image.png", "review": "pending", "meta": {}}]))


def test_pending_dependencies_and_trash_reservation_are_atomic(tmp_path):
    store = JobStore(tmp_path / "jobs")
    runner = Runner(store)
    source = real_source(store)
    payload = {"recipe": "edit-asset", "params": {
        "source": {"jobId": source["id"], "assetId": "a01"}, "plan": {"layers": []}}}
    with TestClient(create_app(store=store, runner=runner, start_runner=False), base_url="http://127.0.0.1:47831") as client:
        derived = client.post("/api/jobs", json=payload).json()
        row = client.get(f"/api/jobs/{source['id']}/storage").json()
        assert row["active"] is False and row["activeUsedBy"] == [derived["id"]]
        assert row["usedBy"] == [derived["id"]]
        reserve_url = f"/api/jobs/{source['id']}/trash/reserve"
        assert client.post(reserve_url, json={}).status_code == 409
        client.post(f"/api/jobs/{derived['id']}/cancel")
        reserved = client.post(reserve_url, json={})
        assert reserved.status_code == 200
        token = reserved.json()["token"]
        assert client.post("/api/jobs", json=payload).status_code == 409
        finish_url = f"/api/jobs/{source['id']}/trash/finish"
        assert client.post(finish_url, json={"token": "x" * 32}).status_code == 400
        assert client.post(finish_url, json={"token": "a" * 32}).status_code == 409
        assert client.post(finish_url, json={"token": token, "failed": True}).status_code == 200
        assert not store.load(source["id"]).get("trashReservation")
        assert client.post("/api/jobs", json=payload).status_code == 201


def test_trash_request_tokens_are_idempotent_and_finished_tokens_cannot_reserve_late(tmp_path):
    store = JobStore(tmp_path / "jobs")
    source = real_source(store)
    base = f"/api/jobs/{source['id']}/trash"
    with TestClient(create_app(store=store, start_runner=False), base_url="http://127.0.0.1:47831") as client:
        request = {"token": "b" * 32, "ownerPid": os.getpid()}
        assert client.post(base + "/reserve", json=request).status_code == 200
        before = store.load(source["id"])["trashReservation"]
        assert client.post(base + "/reserve", json=request).json()["token"] == request["token"]
        assert store.load(source["id"])["trashReservation"] == before
        assert client.post(base + "/finish", json={"token": "c" * 32}).status_code == 409
        assert store.load(source["id"])["trashReservation"] == before
        assert client.post(base + "/finish", json={"token": request["token"]}).status_code == 200
        assert client.post(base + "/finish", json={"token": request["token"]}).status_code == 200
        assert client.post(base + "/reserve", json=request).status_code == 409
        # Even when the reserve has not arrived yet, finishing retires the token.
        late = {"token": "d" * 32, "ownerPid": os.getpid()}
        assert client.post(base + "/finish", json={"token": late["token"], "failed": True}).status_code == 200
        assert client.post(base + "/reserve", json=late).status_code == 409
        for invalid in ({"token": "x" * 32}, {"ownerPid": True}, {"ownerPid": "1"}, {"ownerPid": 0}):
            assert client.post(base + "/reserve", json=invalid).status_code == 400


def test_failed_job_with_live_process_protects_its_input_and_storage(tmp_path):
    import fcntl
    store = JobStore(tmp_path / "jobs")
    source = real_source(store)
    orphan = store.create("edit-asset", {"source": {"jobId": source["id"], "assetId": "a01"}}, "orphan")
    store.update(orphan["id"], lambda job: job.update(state="failed"))
    with (store.job_dir(orphan["id"]) / ".process.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        with TestClient(create_app(store=store, start_runner=False), base_url="http://127.0.0.1:47831") as client:
            storage = client.get(f"/api/jobs/{source['id']}/storage").json()
            assert storage["activeUsedBy"] == [orphan["id"]]
            assert client.get(f"/api/jobs/{orphan['id']}/storage").json()["active"] is True
            rows = {row["jobId"]: row for row in client.get("/api/storage").json()["jobs"]}
            assert rows[orphan["id"]]["active"] is True
            assert rows[source["id"]]["activeUsedBy"] == [orphan["id"]]
            url = f"/api/jobs/{source['id']}/trash/reserve"
            assert client.post(url, json={}).status_code == 409
            fcntl.flock(lease, fcntl.LOCK_UN)
            assert client.get(f"/api/jobs/{source['id']}/storage").json()["activeUsedBy"] == []
            assert client.post(url, json={}).status_code == 200


def test_video_lineage_and_symlink_boundaries(tmp_path):
    store = JobStore(tmp_path / "jobs")
    source = real_source(store)
    video = store.create("image-to-video", {}, "video")
    store.update(video["id"], lambda j: j.update(state="done", assets=[{
        "id": "a01", "kind": "video", "role": "final", "file": "video.mp4", "review": "pending",
        "meta": {"source": {"jobId": source["id"], "assetId": "a01"}}}]))
    with TestClient(create_app(store=store, start_runner=False), base_url="http://127.0.0.1:47831") as client:
        versions = client.get(f"/api/jobs/{video['id']}/assets/a01/versions")
        assert versions.status_code == 200
        assert [v["kind"] for v in versions.json()["versions"]] == ["image", "video"]
        external = tmp_path / "external"
        external.mkdir()
        (external / "outside.txt").write_text("synthetic")
        (store.root / "20261004-000000-abcd").symlink_to(external, target_is_directory=True)
        assert client.get("/files/20261004-000000-abcd/outside.txt").status_code == 404
        uploads = store.root.parent / "uploads"
        uploads.mkdir()
        (uploads / ("a" * 32 + ".png")).symlink_to(external / "outside.txt")
        assert client.get("/uploads/" + "a" * 32).status_code == 404


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


def test_batch_tag_operations_preserve_existing_tags_and_fail_atomically(api):
    client, runner = api
    job = runner.run_job(runner.create("fake", {"subject": "x"})["id"])
    url = f"/api/jobs/{job['id']}/assets/a01/library"
    client.post(url, json={"tags": ["원본", "보관"]})
    added = client.post(url, json={"addTags": ["초안", " 원본 "]}).json()["assets"][0]
    assert added["tags"] == ["원본", "보관", "초안"]
    removed = client.post(url, json={"removeTags": ["보관"]}).json()["assets"][0]
    assert removed["tags"] == ["원본", "초안"]
    assert client.post(url, json={"favorite": True, "addTags": [f"t{i}" for i in range(20)]}).status_code == 400
    unchanged = runner.store.load(job["id"])["assets"][0]
    assert unchanged["tags"] == ["원본", "초안"] and "favorite" not in unchanged
    assert client.post(url, json={"addTags": ["x"], "removeTags": ["y"]}).status_code == 400
    assert client.post(url, json={"addTags": ["x"]}, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/estimates").json() == {"jobs": {}}


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
