import json
import subprocess
import sys

import numpy as np
import pytest
from PIL import Image

from local_assets_engine.jobs import JobStore
from local_assets_engine.presets import PresetError, load_presets
from local_assets_engine.recipes import building_kit
from local_assets_engine.tools import kit_atlas


def synthetic_material(folder, size, *, rows=None, meters=1.0):
    """A tiny scan: horizontal planks with dark gaps when ``rows`` is given, else plain grain."""
    folder.mkdir(parents=True)
    rng = np.random.default_rng(1)
    y = np.arange(size)[:, None]
    grain = 0.55 + 0.1 * np.sin(np.arange(size)[None, :] / 3 + y / 7) + rng.normal(0, 0.02, (size, size))
    height = np.full((size, size), 0.8)
    if rows:
        for seam in np.linspace(0, size, rows, endpoint=False).astype(int):
            height[seam:seam + 2] = 0.05
    color = np.stack([grain * 0.9, grain * 0.7, grain * 0.45], axis=-1)
    normal = np.stack([np.full((size, size), 0.5), np.full((size, size), 0.5), np.ones((size, size))], axis=-1)
    for name, array in (("diff", color), ("nor_gl", normal), ("rough", np.full((size, size), 0.7)),
                        ("ao", np.ones((size, size))), ("disp", height)):
        Image.fromarray(np.clip(array * 255, 0, 255).astype(np.uint8)).save(folder / f"{name}.jpg", quality=95)
    (folder / "material.json").write_text(json.dumps({"sizeMeters": meters}))
    return folder


def test_quarter_turn_keeps_normals_pointing_down_the_slope():
    # Height rising to the right tilts the normal to -x; turned a quarter counter-clockwise the
    # height rises upward in the image and the normal must tilt to -y.
    normal = np.zeros((4, 4, 3), dtype=np.float32)
    normal[..., 0], normal[..., 1], normal[..., 2] = (-0.7 + 1) / 2, 0.5, 0.85
    turned = kit_atlas.rotate_normals(normal)
    assert turned[0, 0, 0] == pytest.approx(0.5) and turned[0, 0, 1] == pytest.approx((-0.7 + 1) / 2)


def test_plank_rows_follow_the_gaps_and_drop_the_wrapping_row():
    height = np.full((400, 400), 0.8, dtype=np.float32)
    for seam in range(10, 400, 40):
        height[seam:seam + 2] = 0.05
    rows = kit_atlas.plank_rows(height)
    assert len(rows) == 9 and all(abs((bottom - top) - 40) <= 2 for top, bottom in rows)


def test_atlas_regions_tile_across_and_stack_planks_timber_shingles(tmp_path):
    planks = synthetic_material(tmp_path / "planks", 512, rows=12, meters=1.6)
    timber = synthetic_material(tmp_path / "timber", 256, meters=0.4)
    atlas, layout = kit_atlas.compose({"planks": planks, "timber": timber}, 256, seed=3)
    assert atlas["diff"].shape == (512, 256, 3) and atlas["nor_gl"].shape == (512, 256, 3)
    regions = layout["regions"]
    assert regions["planks"]["v"] == [0.5, 1.0] and regions["timber"]["v"] == [0.25, 0.5]
    assert regions["shingles"]["v"] == [0.0, 0.25] and len(regions["shingles"]["bands"]) == kit_atlas.SHINGLE_BANDS
    assert all(0.5 <= v0 < v1 <= 1.0 for v0, v1 in regions["planks"]["rows"])
    # Shingle joints are dark and land at the same columns at both ends of the wrap.
    shingles = atlas["diff"][384:, :, 0]
    dark = shingles.mean(axis=0) < np.median(shingles.mean(axis=0)) * 0.7
    assert dark.any()
    files = kit_atlas.save(atlas, layout, tmp_path / "out", "test_kit")
    packed = np.asarray(Image.open(tmp_path / "out" / files["unityMetallicSmoothness"]).convert("RGBA"), dtype=np.int16)
    occlusion = np.asarray(Image.open(tmp_path / "out" / files["occlusion"]).convert("L"), dtype=np.int16)
    assert packed[..., 0].max() == 0 and np.abs(packed[..., 1] - occlusion).max() <= 1


def test_prepare_applies_defaults_and_refuses_unknown_pieces(tmp_path, monkeypatch):
    presets = load_presets()
    store = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(building_kit, "missing", lambda _presets: [])
    params, title = building_kit.prepare({}, presets, store)
    assert params["pieces"] == list(building_kit.PIECES) and params["cellMeters"] == 4.0
    assert params["storyMeters"] == 3.0 and params["wallThickness"] == 0.25 and "wood" in title
    for bad in ({"pieces": ["tower"]}, {"name": "Wood"}, {"referenceImage": str(tmp_path / "none.png")}):
        with pytest.raises(PresetError):
            building_kit.prepare(bad, presets, store)
    monkeypatch.setattr(building_kit, "missing", lambda _presets: ["raw_plank_wall/diff"])
    with pytest.raises(PresetError, match="재질"):
        building_kit.prepare({}, presets, store)


def test_pieces_fit_the_game_grid_and_survive_export(tmp_path):
    planks = synthetic_material(tmp_path / "planks", 512, rows=12, meters=1.6)
    timber = synthetic_material(tmp_path / "timber", 256, meters=0.4)
    atlas = tmp_path / "atlas"
    subprocess.run([sys.executable, "-m", "local_assets_engine.tools.kit_atlas", "--planks", planks, "--timber", timber,
                    "--output", atlas, "--name", "t_kit", "--width", "256"], check=True)
    out = tmp_path / "kit"
    subprocess.run([sys.executable, "-m", "local_assets_engine.tools.kit_build", "--atlas", atlas, "--output", out,
                    "--name", "t", "--pieces", "wall_window,roof_slope,fence_gate", "--samples", "2"], check=True)
    report = json.loads((out / "kit.json").read_text())
    wall, roof = report["pieces"]["wall_window"], report["pieces"]["roof_slope"]
    for piece in report["pieces"].values():
        assert piece["exportProblems"] == [] and piece["triangles"] > 0
        assert (out / piece["files"]["fbx"]).stat().st_size > 0
    # Unity bounds: the wall runs along x to the corner posts (cell + post), from the floor to the story.
    assert wall["boundsUnity"]["min"][0] == pytest.approx(-2.15, abs=0.01)
    assert wall["boundsUnity"]["max"][0] == pytest.approx(2.15, abs=0.01)
    assert wall["boundsUnity"]["min"][1] == pytest.approx(0.0, abs=0.01)
    assert wall["boundsUnity"]["max"][1] == pytest.approx(3.0, abs=0.01)
    assert wall["materials"] == ["M_t_kit", "M_glass"]
    # The slope rises toward the front (+z) from the wall top to two meters higher, plus roof build-up.
    assert 5.0 < roof["boundsUnity"]["max"][1] < 5.4 and 1.95 < roof["boundsUnity"]["max"][2] < 2.12
    assert (out / "review.png").stat().st_size > 0 and (out / "pieces.png").stat().st_size > 0
