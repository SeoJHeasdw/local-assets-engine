import hashlib

import numpy as np
import pytest
from PIL import Image

from local_assets_engine.jobs import JobStore
from local_assets_engine.presets import PresetError, load_presets
from local_assets_engine.recipes.game_asset import GAME_ASSET, prepare
from local_assets_engine.runner import Runner


def master_job(tmp_path, *, variant="master"):
    """A finished job whose mesh is a textured sphere of about 20,000 triangles."""
    import trimesh
    from trimesh.visual.material import PBRMaterial
    from trimesh.visual.texture import TextureVisuals

    store = JobStore(tmp_path / "jobs")
    job = store.create("image-to-3d", {"subject": "striped ball"}, "fixture")
    root = store.job_dir(job["id"]) / "mesh"
    root.mkdir()
    sphere = trimesh.creation.icosphere(subdivisions=5, radius=0.5)
    sphere.apply_translation([0, 0, 0.5])
    direction = sphere.vertices - [0, 0, 0.5]
    uv = np.stack((0.5 + np.arctan2(direction[:, 1], direction[:, 0]) / (2 * np.pi),
                   0.5 + np.arcsin(np.clip(direction[:, 2] / 0.5, -1, 1)) / np.pi), axis=1)
    stripes = np.zeros((256, 256, 3), dtype=np.uint8)
    stripes[(np.arange(256) // 32) % 2 == 0] = (200, 40, 30)
    stripes[(np.arange(256) // 32) % 2 == 1] = (30, 60, 200)
    material = PBRMaterial(baseColorTexture=Image.fromarray(stripes), metallicFactor=0.0, roughnessFactor=0.6)
    sphere.visual = TextureVisuals(uv=uv, material=material)
    sphere.export(root / "asset.glb")
    asset = {"id": "a01", "kind": "mesh", "role": "final", "file": "mesh/asset.glb", "review": "approved",
             "meta": {"variant": variant, "sizeMeters": 1.0}}
    store.update(job["id"], lambda value: value.update(state="done", assets=[asset]))
    return store, job["id"], {"source": {"jobId": job["id"], "assetId": "a01"}}


def test_prepare_applies_defaults_and_refuses_what_it_cannot_bake(tmp_path):
    store, job_id, request = master_job(tmp_path)
    presets = load_presets()
    params, title = prepare(request, presets, store)
    defaults = presets["mesh"]["gameAsset"]
    assert params["targetFaces"] == defaults["targetFaces"] and params["textureSize"] == defaults["textureSize"]
    assert params["name"] == defaults["name"] and params["ao"] is True and "striped ball" in title
    for bad in ("Chest", "_chest", "chest-01", "a" * 41):
        with pytest.raises(PresetError, match="name"):
            prepare({**request, "name": bad}, presets, store)
    with pytest.raises(PresetError, match="source"):
        prepare({}, presets, store)
    store.update(job_id, lambda j: j["assets"][0]["meta"].update(variant="game-asset"))
    with pytest.raises(PresetError, match="품질본"):
        prepare(request, presets, store)
    store.update(job_id, lambda j: j.update(state="failed"))
    with pytest.raises(PresetError, match="완료된"):
        prepare(request, presets, store)


def test_bakes_a_measured_game_mesh_with_maps_and_keeps_the_master(tmp_path):
    store, source_id, request = master_job(tmp_path)
    source = store.job_dir(source_id)
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}
    runner = Runner(store, recipes={GAME_ASSET.id: GAME_ASSET})
    job = runner.run_job(runner.create("game-asset", {
        **request, "targetFaces": 500, "textureSize": 512, "name": "striped_ball"})["id"])
    assert job["state"] == "done", job["error"]
    (asset,) = job["assets"]
    meta = asset["meta"]
    assert asset["review"] == "pending" and meta["variant"] == "game-asset"
    stats = meta["stats"]
    assert stats["sourceTriangles"] == 20480 and stats["triangles"] <= 520
    assert stats["surfaceError"]["maxRelative"] < 0.05
    assert stats["bake"]["metallicConstant"] is True
    error = stats["surfaceError"]
    assert error["lostFraction"] == 0 and error["unfiltered"]["maxMeters"] >= error["maxMeters"] * 0.5
    assert stats["bake"]["missFraction"] < 0.01 and stats["bake"]["exportCheck"]["problems"] == []
    # Nothing on a ball is hidden, so its normals are left as baked.
    assert stats["bake"]["softenedNormalFraction"] < 0.01
    for key in ("reviewFile", "distanceFile", "compareFile"):
        assert store.resolve_file(job["id"], meta[key]).stat().st_size > 0, key
    root = store.job_dir(job["id"]) / "game"
    for name in ("SM_striped_ball.glb", "SM_striped_ball.fbx", "T_striped_ball_basecolor.png",
                 "T_striped_ball_normal.png", "T_striped_ball_roughness.png", "T_striped_ball_occlusion.png",
                 "T_striped_ball_metallicsmoothness.png", "compare.png", "inspection/game-contact.png"):
        assert (root / name).stat().st_size > 0, name
    assert not (root / "T_striped_ball_metallic.png").exists()
    assert not (root / "low.glb").exists()
    # The stripes were carried over from the master texture, not a flat fill.
    with Image.open(root / "T_striped_ball_basecolor.png") as image:
        pixels = np.asarray(image.convert("RGB"), dtype=np.float32)
    covered = pixels[pixels.sum(axis=2) > 30]
    assert covered[:, 0].max() > 150 and covered[:, 2].max() > 150
    # Outside the islands every texel carries its nearest island's color, so mip levels stay clean.
    assert len(covered) == pixels.shape[0] * pixels.shape[1]
    assert abs(stats["bottomOffsetMeters"]) < 0.001
    with Image.open(root / "T_striped_ball_occlusion.png") as image:
        occlusion = np.asarray(image.convert("L"), dtype=np.int16)
    with Image.open(root / "T_striped_ball_metallicsmoothness.png") as image:
        packed = np.asarray(image.convert("RGBA"), dtype=np.int16)
    assert occlusion.min() > 100
    # R metallic, G occlusion (URP's Occlusion Map slot), A smoothness 1 - 0.6, also outside the islands.
    assert packed[..., 0].max() == 0 and np.abs(packed[..., 1] - occlusion).max() <= 1
    assert np.abs(packed[..., 3] - 102).max() <= 3
    with Image.open(root / "T_striped_ball_normal.png") as image:
        normal = np.asarray(image.convert("RGB"), dtype=np.float32) / 255
    # Tangent-space normals of a smooth surface point mostly straight out (blue).
    assert np.median(normal[..., 2]) > 0.8
    for stage in job["stages"]:
        if stage["state"] == "done" and stage["name"].startswith("game-"):
            assert stage["seconds"] > 0 and stage["peakMemoryBytes"] > 0
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*") if p.is_file()}


def test_outer_hull_is_closed_consistently_wound_and_fills_a_ground_opening():
    from local_assets_engine.tools.game_bake import occupancy, outer_surface

    # An open-bottomed box: four walls and a lid, nothing underneath, hollow inside.
    corners = np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)], dtype=float)
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (1, 5, 7, 3)]  # no z=0 face
    triangles = np.array([t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))])
    for ground, solid in ((0.0, True), (None, False)):
        field, origin = occupancy(corners, triangles, 1 / 32, ground)
        vertices, faces = outer_surface(field, origin, 1 / 32)
        edges = np.concatenate([faces[:, [i, (i + 1) % 3]] for i in range(3)])
        directed = {tuple(edge) for edge in edges}
        # Every edge is shared by exactly two faces that walk it in opposite directions.
        assert len(directed) == len(edges) and all((b, a) in directed for a, b in directed)
        a, b, c = vertices[faces[:, 0]], vertices[faces[:, 1]], vertices[faces[:, 2]]
        assert np.einsum("ij,ij->i", a, np.cross(b, c)).sum() > 0
        # Sealed against the ground the hollow box becomes one solid; open, inner walls remain.
        center = tuple(int(v) for v in np.round((np.array([0.5, 0.5, 0.5]) - origin) / (1 / 32)))
        assert bool(field[center] > 0.5) is solid
        # Sealed, the bottom stays on the ground plane, not one eroded voxel above it.
        assert ground is None or abs(vertices[:, 2].min()) < 1e-3 / 32


def test_fill_outside_copies_the_nearest_valid_texel_to_the_edge():
    from local_assets_engine.tools.game_bake import fill_outside, nearest_valid

    valid = np.zeros((8, 8), dtype=bool)
    valid[1:3, 1:3] = valid[5:7, 5:7] = True
    pixels = np.zeros((8, 8, 4), dtype=np.float32)
    pixels[1:3, 1:3] = (1, 0, 0, 1)
    pixels[5:7, 5:7] = (0, 0, 1, 1)
    islands = pixels[valid].copy()
    fill_outside(pixels, nearest_valid(valid))
    assert (pixels[valid] == islands).all()
    assert (pixels[0, 0] == (1, 0, 0, 1)).all() and (pixels[7, 7] == (0, 0, 1, 1)).all()
    assert (pixels[..., :3].sum(axis=2) > 0).all()
    assert nearest_valid(np.ones((4, 4), dtype=bool)) is None and nearest_valid(np.zeros((4, 4), dtype=bool)) is None


def test_soften_hidden_normals_flattens_occluded_tilts_and_keeps_exposed_ones():
    from local_assets_engine.tools.game_bake import NORMAL_Z_FLOOR, soften_hidden_normals

    def encode(*vectors):
        n = np.array(vectors, dtype=np.float32)
        n /= np.linalg.norm(n, axis=1, keepdims=True)
        return np.concatenate(((n + 1) / 2, np.ones((len(n), 1), dtype=np.float32)), axis=1)[None]

    # A face inside a filled gap (tilted down, AO 0.05); the same tilt on an exposed latch side; an
    # exposed normal pointing into the surface; an exposed 63° tilt; a flat texel.
    normal = encode((0, -0.9, 0.2), (0, -0.9, 0.2), (0.8, 0, -0.3), (0, 0.89, 0.45), (0, 0, 1))
    occlusion = np.array([[0.05, 0.9, 0.9, 0.9, 0.9]], dtype=np.float32)
    before = normal.copy()
    share = soften_hidden_normals(normal, occlusion)
    n = normal[0, :, :3] * 2 - 1
    assert n[0, 2] > 0.99
    assert abs(n[1, 2] - NORMAL_Z_FLOOR) < 1e-5 and n[1, 1] < -0.9
    assert abs(n[2, 2] - NORMAL_Z_FLOOR) < 1e-5 and n[2, 0] > 0.9
    assert (normal[0, 3:] == before[0, 3:]).all()
    assert share == 3 / 5


def test_hull_keeps_large_flat_triangles_and_thin_fins():
    import trimesh

    from local_assets_engine.tools.game_bake import occupancy, outer_surface

    # A closed cube of only twelve triangles must still be sampled densely at the default resolution.
    corners = np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)], dtype=float)
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    triangles = np.array([t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))])
    field, origin = occupancy(corners, triangles, 1 / 192, 0.0)
    vertices, faces = outer_surface(field, origin, 1 / 192)
    assert len(faces) and np.ptp(vertices, axis=0).min() > 0.95
    # A 0.5 mm fin on top of a ball is thinner than the erosion; it must survive at hull thickness.
    ball = trimesh.creation.icosphere(subdivisions=3, radius=0.4)
    ball.apply_translation((0, 0, 0.4))
    fin = trimesh.creation.box(extents=(0.3, 0.0005, 0.35))
    fin.apply_translation((0, 0, 0.925))
    master = trimesh.util.concatenate((ball, fin.subdivide().subdivide()))
    field, origin = occupancy(np.asarray(master.vertices), np.asarray(master.faces), 1.1 / 192, 0.0)
    vertices, _ = outer_surface(field, origin, 1.1 / 192)
    assert vertices[:, 2].max() > 1.08


def test_a_groove_the_hull_filled_counts_as_lost_shape_not_hidden(tmp_path):
    import json
    import subprocess
    import sys

    import trimesh

    # A closed block with a slot 1 cm wide, 20 cm long and 30 cm deep cut into its top.
    outer = [(0, 0), (1, 0), (1, 1), (0, 1)]
    inner = [(.495, .4), (.505, .4), (.505, .6), (.495, .6)]
    vertices = np.array([(x, y, 0) for x, y in outer] + [(x, y, 1) for x, y in outer]
                        + [(x, y, 1) for x, y in inner] + [(x, y, .7) for x, y in inner], float)
    quads = [(3, 2, 1, 0), (12, 13, 14, 15)]
    for i in range(4):
        j = (i + 1) % 4
        quads += [(i, j, 4 + j, 4 + i), (4 + i, 4 + j, 8 + j, 8 + i), (8 + i, 8 + j, 12 + j, 12 + i)]
    triangles = np.array([t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))])
    master = trimesh.Trimesh(vertices=vertices, faces=triangles, process=False)
    for _ in range(4):
        master = master.subdivide()
    # glTF is Y-up; the block's Z-up height becomes Y. The change of axes is a rotation, so the winding stays.
    master.vertices = master.vertices[:, [0, 2, 1]] * [1, 1, -1]
    master.export(tmp_path / "master.glb")
    subprocess.run([sys.executable, "-m", "local_assets_engine.tools.game_bake", "reduce",
                    "--master", str(tmp_path / "master.glb"), "--output", str(tmp_path / "low.glb"),
                    "--stats", str(tmp_path / "reduce.json"), "--target-faces", "500"], check=True)
    error = json.loads((tmp_path / "reduce.json").read_text())["surfaceError"]
    # The slot's walls near its mouth saw outside; they are lost shape, not hidden walls, and raise the warning.
    assert error["lostFraction"] >= 0.001 and error["lostMaxMeters"] > 0.02, error
    # Deeper down they see nothing, but the unfiltered distance still shows the whole 30 cm the hull filled.
    assert error["unfiltered"]["maxMeters"] > 0.25, error
