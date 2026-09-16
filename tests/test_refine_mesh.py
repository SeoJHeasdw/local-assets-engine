import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from local_assets_engine.jobs import JobStore
from local_assets_engine.presets import PresetError, load_presets
from local_assets_engine.recipes.refine_mesh import REFINE_MESH, prepare
from local_assets_engine.runner import Runner


def source_job(tmp_path, *, shape="ring"):
    store = JobStore(tmp_path / "jobs")
    job = store.create("image-to-3d", {}, "fixture")
    root = store.job_dir(job["id"]) / "mesh"
    root.mkdir()
    n = 32
    coords = np.indices((n, n, n)).reshape(3, -1).T
    points = coords / n - 0.5
    if shape == "ring":
        u = np.arange(64) / 64 * 2 * np.pi
        v = np.arange(16) / 16 * 2 * np.pi
        u, v = np.meshgrid(u, v, indexing="ij")
        vertices = np.stack(((0.28 + 0.09 * np.cos(v)) * np.cos(u),
                             (0.28 + 0.09 * np.cos(v)) * np.sin(u), 0.09 * np.sin(v)), -1).reshape(-1, 3)
        faces = []
        for a in range(64):
            for b in range(16):
                quad = [a * 16 + b, ((a + 1) % 64) * 16 + b,
                        ((a + 1) % 64) * 16 + (b + 1) % 16, a * 16 + (b + 1) % 16]
                faces.extend([[quad[0], quad[1], quad[2]], [quad[0], quad[2], quad[3]]])
        distance = np.abs(np.sqrt((np.linalg.norm(points[:, :2], axis=1) - 0.28) ** 2 + points[:, 2] ** 2) - 0.09)
        selected = distance < 2 / n
    else:
        xy = np.linspace(-0.3, 0.3, 17)
        vertices = np.array([[x, y, 0] for x in xy for y in xy])
        faces = []
        for a in range(16):
            for b in range(16):
                i = a * 17 + b
                faces.extend([[i, i + 17, i + 18], [i, i + 18, i + 1]])
        selected = (np.abs(points[:, 2]) < 2 / n) & (np.abs(points[:, :2]).max(axis=1) < 0.34)
    coords = coords[selected].astype(np.int32)
    attrs = np.tile([0.7, 0.3, 0.1, 0.6, 0.5, 1], (len(coords), 1)).astype(np.float32)
    np.savez(root / "source.npz", vertices=np.asarray(vertices, dtype=np.float32),
             faces=np.asarray(faces, dtype=np.int32), coords=coords, attrs=attrs,
             origin=np.array([-0.5] * 3), voxel_size=1 / n)
    Image.new("RGBA", (16, 16), "orange").save(root / "input.png")
    asset = {"id": "a01", "kind": "mesh", "review": "approved", "meta": {
        "seed": 7, "pipelineType": "512", "sizeMeters": 1,
        "sourceStateFile": "mesh/source.npz", "processingVersion": 2,
    }}
    store.update(job["id"], lambda value: value.update(state="done", assets=[asset]))
    return store, job["id"], {"source": {"jobId": job["id"], "assetId": "a01"}}


def test_refine_uses_current_quality_settings_and_keeps_generation_identity(tmp_path):
    store, job_id, request = source_job(tmp_path)
    params, _ = prepare({**request, "meshSeed": 999, "pipelineType": "1024"}, load_presets(), store)
    assert params["meshSeed"] == 7 and params["pipelineType"] == "512"
    assert params["targetFaces"] == 1000000 and params["textureSize"] == 4096
    store.update(job_id, lambda j: j["assets"][0]["meta"].pop("sourceStateFile"))
    with pytest.raises(PresetError, match="감축 전 원본"):
        prepare(request, load_presets(), store)


def test_refine_keeps_original_and_publishes_measured_unapproved_variants(tmp_path):
    store, source_id, request = source_job(tmp_path)
    source = store.job_dir(source_id)
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}
    runner = Runner(store, recipes={REFINE_MESH.id: REFINE_MESH})
    job = runner.run_job(runner.create("refine-mesh", {
        **request, "targetFaces": 3000, "textureSize": 512, "gameFaces": 1000, "gameTextureSize": 512,
    })["id"])
    assert job["state"] == "done", job["error"]
    assert len(job["assets"]) == 2
    assert {a["meta"]["variant"] for a in job["assets"]} == {"master", "game"}
    for asset in job["assets"]:
        assert asset["review"] == "pending"
        assert store.resolve_file(job["id"], asset["preview"]).stat().st_size > 0
        assert store.resolve_file(job["id"], asset["meta"]["inspectionFile"]).stat().st_size > 0
        assert asset["meta"]["stats"]["facesIn"] == asset["meta"]["stats"]["facesOut"]
    for stage in job["stages"]:
        if stage["state"] == "done":
            assert stage["seconds"] > 0 and stage["peakMemoryBytes"] > 0
    assert not any(s["name"] == "mesh" for s in job["stages"])
    master, game = job["assets"]
    assert master["meta"]["stats"]["normalization"] == game["meta"]["stats"]["normalization"]
    assert "lod" in game["meta"]["stats"]
    assert game["meta"]["stats"]["lod"]["topology"] == {
        "boundaryEdges": 0, "nonManifoldEdges": 0, "inconsistentWindingEdges": 0,
    }
    import trimesh
    for asset in job["assets"]:
        optimized = asset["meta"]["optimizedFile"]
        if optimized:
            original = trimesh.load_scene(store.resolve_file(job["id"], asset["file"]), process=False).to_mesh()
            packed = trimesh.load_scene(store.resolve_file(job["id"], optimized), process=False).to_mesh()
            np.testing.assert_array_equal(np.unique(original.vertices, axis=0), np.unique(packed.vertices, axis=0))
    geom = np.load(store.job_dir(job["id"]) / "mesh/surface/master.npz")
    # A quality fix must not fill the ring's intentional central hole.
    assert np.linalg.norm(geom["vertices"][:, :2], axis=1).min() > 0.1
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}


def test_surface_reconstruction_keeps_an_open_thin_sheet(tmp_path):
    from local_assets_engine.paths import trellis_python
    from local_assets_engine.recipes.base import Recipe

    store, source_id, _ = source_job(tmp_path, shape="sheet")
    source = store.job_dir(source_id) / "mesh/source.npz"
    worker = Path(__file__).resolve().parents[1] / "src/local_assets_engine/workers/quality_surface.py"
    def run(ctx):
        with ctx.stage("surface", "thin surface") as stage:
            stage.run([trellis_python(), worker, "--source", source, "--output", ctx.dir / "surface",
                       "--target-faces", 2500, "--game-faces", 0])
    recipe = Recipe("fixture", "fixture", lambda p, _, s: (p, "fixture"), run)
    runner = Runner(store, recipes={"fixture": recipe})
    job = runner.run_job(runner.create("fixture", {})["id"])
    assert job["state"] == "done", job["error"]
    geom = np.load(store.job_dir(job["id"]) / "surface/master.npz")
    bounds = np.ptp(geom["vertices"], axis=0)
    assert bounds[0] > 0.55 and bounds[1] > 0.55 and bounds[2] < 0.09


def test_default_generation_only_publishes_the_quality_master(tmp_path):
    store, source_id, request = source_job(tmp_path)
    runner = Runner(store, recipes={REFINE_MESH.id: REFINE_MESH})
    created = runner.create('refine-mesh', {**request, 'targetFaces':3000, 'textureSize':512})
    assert created['params']['gameFaces'] == 0
    job = runner.run_job(created['id'])
    assert job['state'] == 'done', job['error']
    assert len(job['assets']) == 1 and job['assets'][0]['meta']['variant'] == 'master'
    assert not any(s['name'] == 'lod' or s['name'].endswith('-game') for s in job['stages'])
