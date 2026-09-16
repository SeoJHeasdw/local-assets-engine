import hashlib
import json
import struct

import numpy as np
import pytest
import trimesh
from PIL import Image

from local_assets_engine.jobs import JobStore
from local_assets_engine.presets import PresetError, load_presets
from local_assets_engine.recipes.repair_mesh import REPAIR_MESH, prepare
from local_assets_engine.runner import Runner


@pytest.fixture
def legacy_job(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create("text-to-3d", {"subject": "test chest"}, "test chest")
    root = store.job_dir(job["id"])
    (root / "mesh").mkdir()
    mesh = trimesh.creation.box(extents=(1, 2, 3))
    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.new("RGB", (2, 2), "red"), metallicFactor=0, roughnessFactor=0.8,
    )
    mesh.visual = trimesh.visual.TextureVisuals(uv=np.full((len(mesh.vertices), 2), 0.25), material=material)
    mesh.export(root / "mesh/raw.glb")
    Image.new("RGBA", (2, 2), "red").save(root / "mesh/input.png")
    (root / "job.log").write_text("Baking PBR textures via KDTree (1024x1024)...\n")
    meta = {
        "rawFile": "mesh/raw.glb", "pipelineType": "512", "textureSize": 1024,
        "targetFaces": 12, "sizeMeters": 2, "seed": 123,
    }
    asset = {"id": "a02", "kind": "mesh", "preview": "mesh/input.png", "meta": meta, "review": "approved"}
    store.update(job["id"], lambda j: j.update(state="done", assets=[asset]))
    return store, job["id"], {"source": {"jobId": job["id"], "assetId": "a02"}}


def test_repair_preserves_source_parameters_and_refuses_unknown_provenance(legacy_job):
    store, job_id, params = legacy_job
    normalized, _ = prepare(params, load_presets(), store)
    assert (normalized["meshSeed"], normalized["targetFaces"], normalized["sizeMeters"]) == (123, 12, 2)
    (store.job_dir(job_id) / "job.log").write_text("Baking PBR textures via Metal...")
    with pytest.raises(PresetError, match="KDTree"):
        prepare(params, load_presets(), store)
    (store.job_dir(job_id) / "job.log").write_text(
        "[local-assets] KDTree GLB export v1\nBaking PBR textures via KDTree..."
    )
    with pytest.raises(PresetError, match="이미"):
        prepare(params, load_presets(), store)


def test_repair_rejects_missing_assets_and_path_escape(legacy_job):
    store, job_id, params = legacy_job
    with pytest.raises(PresetError, match="source"):
        prepare({}, load_presets(), store)
    with pytest.raises(PresetError, match="찾지"):
        prepare({"source": {"jobId": job_id, "assetId": "missing"}}, load_presets(), store)
    store.update(job_id, lambda j: j["assets"][0]["meta"].update(rawFile="../../outside.glb"))
    with pytest.raises(PresetError, match="찾지"):
        prepare(params, load_presets(), store)


def test_repair_runs_measured_postprocessing_and_keeps_original_review(legacy_job):
    store, source_id, params = legacy_job
    source = store.job_dir(source_id)
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}
    runner = Runner(store, recipes={REPAIR_MESH.id: REPAIR_MESH})
    job = runner.run_job(runner.create("repair-mesh", params)["id"])
    assert job["state"] == "done", job["error"]
    stages = {s["name"]: s for s in job["stages"]}
    for name in ("repair", "post"):
        assert stages[name]["seconds"] > 0
        assert stages[name]["peakMemoryBytes"] > 0
        assert len(stages[name]["processes"]) == 1
    assert "generate" not in stages and "mesh" not in stages
    asset = job["assets"][0]
    assert asset["review"] == "pending"
    assert asset["meta"]["source"] == params["source"]
    assert asset["meta"]["stats"]["topology"]["warnings"] == []
    np.testing.assert_allclose(asset["meta"]["stats"]["dimensionsMeters"], [2/3, 4/3, 2], atol=1e-4)
    output = store.resolve_file(job["id"], asset["file"])
    data = output.read_bytes()
    doc = json.loads(data[20:20 + struct.unpack_from("<I", data, 12)[0]])
    assert all(material.get("doubleSided") for material in doc["materials"])
    loaded = trimesh.load_scene(output, process=False)
    np.testing.assert_allclose(loaded.bounds[:, 1], [0, 2], atol=1e-5)
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}
    with pytest.raises(PresetError, match="이전"):
        prepare({"source": {"jobId": job["id"], "assetId": asset["id"]}}, load_presets(), store)
