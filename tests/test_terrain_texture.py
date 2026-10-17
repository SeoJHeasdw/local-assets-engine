import json

import numpy as np
import pytest
from PIL import Image

from local_assets_engine.jobs import JobStore
from local_assets_engine.presets import PresetError, load_presets
from local_assets_engine.recipes import terrain_texture
from local_assets_engine.tools import terrain_tile


def periodic_field(size, wavelength, seed):
    """Smooth noise that wraps at every edge, scaled to 0..1."""
    noise = terrain_tile.periodic_noise(size, wavelength, np.random.default_rng(seed))
    return (noise - noise.min()) / (noise.max() - noise.min())


def synthetic_scan(folder, size, *, blob=False, meters=1.0, light=0.0, seed=0):
    """A seamless scan: mottled green-brown color, matching height and normals, optionally one bright stone
    and a sun from +x baked into the color."""
    folder.mkdir(parents=True)
    height = periodic_field(size, size / 6, seed)
    gy, gx = np.gradient(height * 8)
    normal = np.stack([-gx, gy, np.ones_like(height)], axis=-1)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    color = np.stack([0.25 + 0.2 * height, 0.35 + 0.15 * height, 0.12 + 0.05 * height], axis=-1)
    color *= (1 + light * normal[..., 0])[..., None]
    if blob:
        rows, cols = np.mgrid[0:size, 0:size]
        stone = (rows - size // 2) ** 2 + (cols - size // 2) ** 2 < (size // 14) ** 2
        color[stone] = 0.95
        height[stone] = 1.0
    maps = {"diff": color, "nor_gl": normal * 0.5 + 0.5, "rough": np.full((size, size), 0.8), "ao": np.full((size, size), 0.9),
            "disp": height}
    for name, array in maps.items():
        Image.fromarray(np.clip(array * 255, 0, 255).astype(np.uint8)).save(folder / f"{name}.jpg", quality=97)
    (folder / "material.json").write_text(json.dumps({"sizeMeters": meters}))
    return folder


def spec(layer, *, size=256, tile=2.0, patch=None, grade=None):
    return {"name": "test", "tileMeters": tile, "size": size, "seed": 3, "patchMeters": 0.5, "flatten": 0.5,
            "flattenMeters": 0.6, "layer": layer, "patch": patch, "grade": grade or {}}


def test_resampling_keeps_a_map_seamless():
    field = periodic_field(96, 20, 1)
    resized = terrain_tile.resample_periodic(field, 160)
    assert resized.shape == (160, 160)
    assert terrain_tile.wrap_seam_ratio(resized) < 1.3


def test_quilted_tile_wraps_at_its_new_period_and_skips_a_stand_out_stone(tmp_path):
    folder = synthetic_scan(tmp_path / "scan", 256, blob=True, meters=1.0)
    maps, stats = terrain_tile.build(spec({"id": "scan", "dir": str(folder), "sizeMeters": 1.0}, size=256, tile=2.5))
    layer = stats["layers"][0]
    # The scan covers 1 m; the tile 2.5 m is not a whole number of scans, so it was quilted to its own period.
    assert layer["scanPx"] == round(256 / 2.5) and layer["quilt"]["patchesPerRow"] >= 2
    assert stats["wrapSeamRatio"] < 1.3
    assert np.allclose(np.linalg.norm(maps["normal"], axis=-1), 1, atol=1e-3)
    # The stone (color 0.95) never made it into the tile.
    assert terrain_tile.luminance(terrain_tile.linear_to_srgb(maps["color"])).max() < 0.85


def test_a_scan_as_large_as_the_tile_is_still_quilted_around_its_stand_out_stone(tmp_path):
    folder = synthetic_scan(tmp_path / "scan", 256, blob=True, meters=2.0)
    maps, stats = terrain_tile.build(spec({"id": "scan", "dir": str(folder), "sizeMeters": 2.0}, size=256, tile=2.0))
    assert stats["layers"][0]["quilt"]["patchesPerRow"] >= 2 and stats["wrapSeamRatio"] < 1.3
    assert terrain_tile.luminance(terrain_tile.linear_to_srgb(maps["color"])).max() < 0.85


def test_baked_sun_is_divided_out(tmp_path):
    # 0.12 on the stored sRGB color is about 0.26 in linear light, like the strongest scans seen.
    folder = synthetic_scan(tmp_path / "scan", 128, light=0.12)
    maps = terrain_tile.load_scan(folder, 128)
    slope = terrain_tile.directional_light(maps["color"], maps["normal"])
    assert slope[0] > 0.1
    terrain_tile.remove_directional(maps, slope)
    assert abs(terrain_tile.directional_light(maps["color"], maps["normal"])[0]) < 0.03


def test_second_layer_shows_through_for_exactly_its_share():
    rng = np.random.default_rng(2)
    base, patch = periodic_field(128, 16, 3), periodic_field(128, 16, 4)
    weight = terrain_tile.blend_weight(base, patch, terrain_tile.periodic_noise(128, 30, rng), 0.3)
    assert weight.mean() == pytest.approx(0.3, abs=0.005)
    assert weight.min() >= 0 and weight.max() <= 1


def test_grade_turns_green_toward_yellow_and_leaves_brown_alone():
    srgb = np.array([[[0.25, 0.6, 0.15], [0.5, 0.35, 0.2]]], dtype=np.float32)
    graded = terrain_tile.linear_to_srgb(terrain_tile.grade(terrain_tile.srgb_to_linear(srgb), hue_shift=15, max_saturation=0.6))
    before, after = terrain_tile.rgb_to_hsv(srgb), terrain_tile.rgb_to_hsv(graded)
    # Full turn at hue 100° (grass green), less toward yellow and blue.
    turn = 15 * max(0.0, 1 - abs(float(before[0, 0, 0]) - 100) / 55)
    assert after[0, 0, 0] == pytest.approx(before[0, 0, 0] - turn, abs=0.5) and turn > 10
    assert after[0, 0, 1] <= 0.6 + 1e-3
    assert after[0, 1, 0] == pytest.approx(before[0, 1, 0], abs=0.5)


def test_tool_writes_unity_maps_and_meadow_patches(tmp_path):
    grass = synthetic_scan(tmp_path / "grass", 128, meters=0.7, seed=1)
    soil = synthetic_scan(tmp_path / "soil", 128, meters=1.0, seed=2)
    request = spec({"id": "grass", "dir": str(grass), "sizeMeters": 0.7, "roughness": [0.6, 0.85]}, size=256, tile=2.0,
                   patch={"id": "soil", "dir": str(soil), "sizeMeters": 1.0, "coverage": 0.3, "noiseMeters": 0.3},
                   grade={"hueShift": 8, "maxSaturation": 0.6})
    (tmp_path / "spec.json").write_text(json.dumps(request))
    assert terrain_tile.main(["--spec", str(tmp_path / "spec.json"), "--output", str(tmp_path / "out")]) == 0
    report = json.loads((tmp_path / "out" / "tile.json").read_text())
    assert report["patchCoverage"] == pytest.approx(0.3, abs=0.01) and report["wrapSeamRatio"] < 1.5
    mask = np.asarray(Image.open(tmp_path / "out" / report["files"]["mask"]))
    assert mask.shape == (256, 256, 4) and mask[..., 0].max() == 0
    # Smoothness is 1 - roughness; the grass roughness was moved into 0.6..0.85 before blending.
    assert 255 * 0.1 < mask[..., 3].mean() < 255 * 0.45
    normal = np.asarray(Image.open(tmp_path / "out" / report["files"]["normal"]), dtype=np.float32) / 255 * 2 - 1
    assert np.abs(np.linalg.norm(normal, axis=-1) - 1).mean() < 0.02
    assert Image.open(tmp_path / "out" / report["files"]["basecolor"]).mode == "RGB"


def test_prepare_takes_preset_values_and_refuses_unknown_scans(tmp_path, monkeypatch):
    presets = load_presets()
    store = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(terrain_texture, "missing", lambda _presets, _scans=None: [])
    params, title = terrain_texture.prepare({"preset": "temperate-meadow"}, presets, store)
    assert params["name"] == "terrain_meadow" and params["tileMeters"] == 3.0 and params["size"] == 2048
    assert params["layer"]["id"] == "Grass004" and params["patch"]["coverage"] == 0.3 and "들판" in title
    alone, _ = terrain_texture.prepare({"preset": "temperate-meadow", "patch": None, "tileMeters": 4}, presets, store)
    assert alone["patch"] is None and alone["tileMeters"] == 4.0
    for bad in ({}, {"preset": "lava"}, {"preset": "beach-sand", "layer": "boulder_01"},
                {"preset": "beach-sand", "grade": {"hue": 3}}, {"preset": "beach-sand", "name": "Sand"}):
        with pytest.raises(PresetError):
            terrain_texture.prepare(bad, presets, store)
    monkeypatch.setattr(terrain_texture, "missing", lambda _presets, _scans=None: ["dense_sand/diff"])
    with pytest.raises(PresetError, match="materials fetch"):
        terrain_texture.prepare({"preset": "beach-sand"}, presets, store)


def test_patches_come_from_inside_the_scan_not_across_its_edge():
    rng = np.random.default_rng(0)
    offsets = terrain_tile.patch_offsets(rng, 300, 100, 5000)
    assert offsets.min() >= 4 and offsets.max() + 100 <= 300 - 4
    # A scan smaller than a patch has no inside to pick from; it wraps.
    assert terrain_tile.patch_offsets(rng, 90, 100, 10).max() < 90
