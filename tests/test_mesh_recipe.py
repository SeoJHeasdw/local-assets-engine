from local_assets_engine.measure import StageFailed
from local_assets_engine.recipes.mesh import explain_trellis_failure
from local_assets_engine.recipes.mesh import prepare_mesh_params
from local_assets_engine.presets import load_presets, PresetError
import pytest


def failure(*tail, code=1):
    return StageFailed("종료 코드 1", code=code, tail=list(tail))


def test_a_killed_process_is_explained_as_memory_pressure():
    message = explain_trellis_failure(failure("time: signal: Invalid argument", "warnings.warn(...)"))
    assert "메모리" in message and "512" in message


def test_missing_dinov3_access_points_at_the_setup_step():
    message = explain_trellis_failure(failure("huggingface_hub.errors.GatedRepoError: 403"))
    assert "DINOv3" in message and "SETUP" in message


def test_the_gpu_watchdog_is_explained_separately():
    message = explain_trellis_failure(failure("kIOGPUCommandBufferCallbackErrorTimeout", code=2))
    assert "GPU" in message


def test_anything_else_keeps_the_original_message():
    assert "TRELLIS.2" in explain_trellis_failure(failure("모델 파일이 깨졌습니다"))


def test_mesh_audit_is_opt_in_and_preserves_generation_settings():
    values = {"meshSeed": 123, "targetFaces": 30000, "textureSize": 1024, "pipelineType": "512"}
    baseline = prepare_mesh_params(values, load_presets())
    audited = prepare_mesh_params({**values, "audit": True}, load_presets())
    assert baseline["audit"] is False
    assert audited == {**baseline, "audit": True}
    with pytest.raises(PresetError):
        prepare_mesh_params({"audit": "maybe"}, load_presets())


def test_topology_and_face_budget_checks_become_warn_findings(tmp_path):
    from local_assets_engine.jobs import JobStore
    from local_assets_engine.recipes.base import Recipe
    from local_assets_engine.recipes.mesh import report_mesh_checks
    from local_assets_engine.runner import Runner

    def run(ctx):
        mesh = ctx.dir / "mesh"
        mesh.mkdir()
        (mesh / "asset.game.glb").write_bytes(b"glb")
        asset = ctx.add_asset(kind="mesh", role="final", file=mesh / "asset.game.glb")
        flawed = {"topology": {"boundaryEdges": 12, "nonManifoldEdges": 0, "inconsistentWindingEdges": 3,
                               "duplicateFaces": 0}}
        report_mesh_checks(ctx, asset, flawed, where="게임용 메시", evidence=[mesh / "asset.game.stats.json"],
                           faces=(1000, 1500))
        clean = {"topology": {"boundaryEdges": 0, "nonManifoldEdges": 0, "inconsistentWindingEdges": 0,
                              "duplicateFaces": 0}}
        report_mesh_checks(ctx, asset, clean, where="품질본 메시", evidence=[], faces=(1000, 1000))
        report_mesh_checks(ctx, asset, clean, where="품질본 메시", evidence=[], faces=(0, 5000))

    store = JobStore(tmp_path / "jobs")
    runner = Runner(store, recipes={"fake": Recipe("fake", "fake", lambda p, _presets, _store: (p, "t"), run)},
                    presets_loader=dict, machine_lock_path=tmp_path / ".machine.lock")
    job = runner.run_job(runner.create("fake", {})["id"])
    assert job["state"] == "done", job["error"]
    assert [(f["severity"], f["code"], f["params"]) for f in job["findings"]] == [
        ("warn", "mesh.open_boundary", {"count": 12}),
        ("warn", "mesh.inconsistent_winding", {"count": 3}),
        ("warn", "mesh.faces_over_target", {"targetFaces": 1000, "triangles": 1500}),
    ]
    assert {(f["where"], f["assetId"], tuple(f["evidence"])) for f in job["findings"]} == {
        ("게임용 메시", "a01", ("mesh/asset.game.stats.json",))}
    assert job["assets"][0]["review"] == "pending"
