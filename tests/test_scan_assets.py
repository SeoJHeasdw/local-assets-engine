"""Foliage cards, pinned CC0 scan downloads, and scanned models as game-asset masters."""

import hashlib
import io
import json
import subprocess
import sys
import zipfile

import numpy as np
import pytest
from PIL import Image

from local_assets_engine import imaging, materials
from local_assets_engine.jobs import JobStore
from local_assets_engine.presets import PresetError, load_presets
from local_assets_engine.recipes import scan_mesh
from local_assets_engine.tools.scan_prepare import choose_cut


def grass_on_white(width=200, height=240):
    """Raw image and cutout: a dark green fan of blades on white, with soft blade edges mixed with the white."""
    rgb = np.ones((height, width, 3), dtype=np.float32)
    alpha = np.zeros((height, width), dtype=np.float32)
    for k in range(-4, 5):
        for y in range(40, 220):
            x = 70 + k * 3 * (220 - y) / 180 + 30
            for dx, cover in ((-1, 0.4), (0, 1.0), (1, 0.4)):
                column = int(round(x)) + dx
                alpha[y, column] = max(alpha[y, column], cover)
    green = np.array([0.15, 0.4, 0.1], dtype=np.float32)
    rgb = rgb * (1 - alpha[..., None]) + green * alpha[..., None]
    raw = Image.fromarray((rgb * 255).astype(np.uint8), "RGB")
    cutout = raw.copy()
    cutout.putalpha(Image.fromarray((alpha * 255).astype(np.uint8), "L"))
    return raw, cutout


def test_card_takes_the_white_out_of_soft_edges_and_stands_on_the_bottom_center():
    raw, cutout = grass_on_white()
    clean = imaging.decontaminate(cutout, imaging.border_color(raw))
    pixels = np.asarray(clean, dtype=np.float32) / 255
    soft = (pixels[..., 3] > 0.3) & (pixels[..., 3] < 0.6)
    # Soft edge pixels were 60% white; without it they are the blade's green again.
    assert soft.any() and pixels[soft, :3].mean(axis=0) == pytest.approx([0.15, 0.4, 0.1], abs=0.03)
    card, info = imaging.fit_card(clean, 256, 256, fill=0.9)
    box = imaging.alpha_bbox(card, threshold=24)
    assert box[3] == 256 and info["fill"] == pytest.approx(0.9, abs=0.01)
    foot = np.asarray(card)[-4:, :, 3].astype(float)
    assert (foot.sum(axis=0) * np.arange(256)).sum() / foot.sum() == pytest.approx(128, abs=3)


def test_card_grade_darkens_the_base_and_warms_the_tips_and_fills_clear_texels():
    _raw, cutout = grass_on_white()
    card, _ = imaging.fit_card(cutout, 256, 256)
    graded = np.asarray(imaging.grade_card(card, 0.6, 0.12), dtype=np.float32)
    before = np.asarray(card, dtype=np.float32)
    solid = before[..., 3] > 250
    rows = np.nonzero(solid.any(axis=1))[0]
    low, high = solid & (np.arange(256)[:, None] > rows[-1] - 10), solid & (np.arange(256)[:, None] < rows[0] + 10)
    assert graded[low, 1].mean() < before[low, 1].mean() * 0.9
    assert (graded[high, 0] - graded[high, 2]).mean() > (before[high, 0] - before[high, 2]).mean()
    filled = np.asarray(imaging.fill_transparent(Image.fromarray(graded.astype(np.uint8), "RGBA")))
    clear = filled[..., 3] == 0
    # Clear texels carry the plant's green, not black, so filtering leaves no dark rim.
    assert filled[clear, 1].mean() > 60 and np.array_equal(filled[..., 3], graded[..., 3].astype(np.uint8))


def test_fetch_takes_pinned_members_out_of_an_archive_and_refuses_a_changed_one(tmp_path, monkeypatch):
    color, normal = b"color-bytes", b"normal-bytes"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("X_Color.jpg", color)
        packed.writestr("X_NormalGL.jpg", normal)
        packed.writestr("../evil.jpg", b"no")
    data = archive.getvalue()
    md5 = lambda value: hashlib.md5(value).hexdigest()  # noqa: E731
    presets = {"scanLibrary": {"X": {
        "source": "ambientcg", "kind": "texture", "id": "X", "license": "CC0-1.0", "sizeMeters": 1.0,
        "archive": {"url": "https://example.invalid/X.zip", "md5": md5(data)},
        "files": {"diff": {"member": "X_Color.jpg", "md5": md5(color)}, "nor_gl": {"member": "X_NormalGL.jpg", "md5": md5(normal)}},
    }}}
    monkeypatch.setattr(materials, "MATERIALS_DIR", tmp_path)
    monkeypatch.setattr(materials, "_download", lambda _url: data)
    assert materials.missing(presets, ["X"]) == ["X/material.json", "X/diff", "X/nor_gl"]
    written = materials.fetch(presets, ["X"])
    assert sorted(path.name for path in written) == ["diff.jpg", "nor_gl.jpg"] and not (tmp_path.parent / "evil.jpg").exists()
    assert materials.missing(presets, ["X"]) == []
    assert json.loads((tmp_path / "X" / "material.json").read_text())["license"] == "CC0-1.0"
    presets["scanLibrary"]["X"]["files"]["diff"]["md5"] = md5(b"other")
    (tmp_path / "X" / "diff.jpg").unlink()
    with pytest.raises(SystemExit):
        materials.fetch(presets, ["X"])
    with pytest.raises(ValueError):
        materials.file_path({"id": "X"}, "../outside.jpg")


def test_every_pinned_scan_names_its_license_and_terrain_presets_use_texture_scans():
    presets = load_presets()
    library = presets["scanLibrary"]
    for scan in library.values():
        assert scan["license"].startswith("CC0") and scan["page"].startswith("https://") and scan["author"]
    for preset in presets["terrain"]["presets"]:
        assert library[preset["layer"]]["kind"] == "texture"


def test_cut_height_matches_the_requested_height_to_width():
    # A dome 2 m wide and 1 m high: cutting it lower keeps the width, cutting higher narrows the base.
    angles = np.linspace(0, np.pi / 2, 40)
    ring = np.linspace(0, 2 * np.pi, 60)
    coords = np.array([(np.cos(a) * np.cos(r), np.cos(a) * np.sin(r), np.sin(a)) for a in angles for r in ring])
    fraction, ratio = choose_cut(coords, 0.05, None)
    assert fraction == 0.05 and ratio == pytest.approx(0.95 / 2, rel=0.05)
    fraction, ratio = choose_cut(coords, 0.05, 0.3)
    assert fraction > 0.3 and ratio == pytest.approx(0.3, rel=0.1)


def rock_scan(tmp_path):
    """A textured, lumpy closed rock as glTF (Y up), about 1.2 × 0.8 × 0.7 m, lying on its lowest point."""
    import trimesh
    from trimesh.visual.material import PBRMaterial
    from trimesh.visual.texture import TextureVisuals

    rock = trimesh.creation.icosphere(subdivisions=4, radius=0.5)
    rng = np.random.default_rng(5)
    rock.vertices *= 1 + 0.08 * rng.standard_normal((len(rock.vertices), 1))
    rock.vertices *= [1.2, 0.8, 0.7]
    rock.vertices[:, 2] -= rock.vertices[:, 2].min()
    direction = rock.vertices - rock.vertices.mean(axis=0)
    uv = np.stack((0.5 + np.arctan2(direction[:, 1], direction[:, 0]) / (2 * np.pi),
                   0.5 + np.arcsin(np.clip(direction[:, 2] / np.linalg.norm(direction, axis=1), -1, 1)) / np.pi), axis=1)
    orange = np.zeros((64, 64, 3), dtype=np.uint8)
    orange[:] = (190, 110, 50)
    rock.visual = TextureVisuals(uv=uv, material=PBRMaterial(baseColorTexture=Image.fromarray(orange), roughnessFactor=0.9))
    rock.vertices = rock.vertices[:, [0, 2, 1]] * [1, 1, -1]
    # Like a scan's glTF, vertices are split where UVs are: every edge is a seam until welded.
    rock.unmerge_vertices()
    folder = tmp_path / "rock"
    folder.mkdir()
    rock.export(folder / "rock.gltf")
    return folder / "rock.gltf"


def test_prepared_scan_is_cut_flat_sized_centered_and_graded(tmp_path):
    import trimesh

    gltf = rock_scan(tmp_path)
    master, stats_path = tmp_path / "master.glb", tmp_path / "prepare.json"
    subprocess.run([sys.executable, "-m", "local_assets_engine.tools.scan_prepare", "--gltf", str(gltf), "--output", str(master),
                    "--stats", str(stats_path), "--height", "0.4", "--width", "2.0", "--bury", "0.05",
                    "--saturation", "0.4"], check=True)
    stats = json.loads(stats_path.read_text())
    loaded = trimesh.load(master, force="mesh")
    low, high = loaded.bounds
    # glTF Y is up: 0.4 m high on the ground, 2.0 m along X, centered over the origin.
    assert low[1] == pytest.approx(0, abs=1e-4) and high[1] - low[1] == pytest.approx(0.4, abs=1e-3)
    assert high[0] - low[0] == pytest.approx(2.0, abs=1e-3) and abs(low[0] + high[0]) < 1e-3 and abs(low[2] + high[2]) < 1e-3
    # Flat bottom: the cap closes the cut, so faces covering most of the footprint lie in the ground plane
    # and face down (the cut also leaves thin side slivers right above it).
    bottom = np.abs(loaded.triangles_center[:, 1] - low[1]) < 1e-4
    down = bottom & (loaded.face_normals[:, 1] < -0.99)
    assert loaded.area_faces[down].sum() > 0.98 * loaded.area_faces[bottom].sum() and stats["capFaces"] > 0
    assert loaded.area_faces[down].sum() > 0.5 * (high[0] - low[0]) * (high[2] - low[2])
    assert stats["cutFraction"] > 0.05 and stats["stretchXY"] == pytest.approx(1, abs=0.35)
    # Welded before the cut: one closed surface, and the cap only closes the cut.
    topology = stats["topology"]
    assert topology["boundaryEdges"] == topology["nonManifoldEdges"] == topology["duplicateFaces"] == 0, topology
    assert stats["capFaces"] < 600
    color = np.asarray(loaded.visual.material.baseColorTexture.convert("RGB"), dtype=np.float32)
    hsv = imaging_hsv(color / 255)
    assert hsv[..., 1].mean() == pytest.approx(0.4 * (190 - 50) / 190, abs=0.05)


def imaging_hsv(rgb):
    from local_assets_engine.tools.terrain_tile import rgb_to_hsv

    return rgb_to_hsv(rgb.astype(np.float32))


def test_prepare_scan_mesh_needs_a_model_scan_and_a_height(tmp_path, monkeypatch):
    presets = load_presets()
    store = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(scan_mesh, "missing", lambda _presets, _scans=None: [])
    params, title = scan_mesh.prepare({"scan": "boulder_01", "heightMeters": 1.7, "widthMeters": 3}, presets, store)
    assert params["gltfPath"].endswith("boulder_01/boulder_01_4k.gltf") and params["scan"]["license"] == "CC0"
    assert params["widthMeters"] == 3.0 and params["bury"] == 0.05 and "boulder_01" in title
    for bad in ({"scan": "dense_sand", "heightMeters": 1}, {"scan": "boulder_01"}, {"heightMeters": 1},
                {"scan": "boulder_01", "heightMeters": 1, "name": "Rock"}):
        with pytest.raises(PresetError):
            scan_mesh.prepare(bad, presets, store)
