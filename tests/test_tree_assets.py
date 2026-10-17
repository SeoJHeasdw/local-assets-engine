"""Trees from CC0 scans: leaf elements, the bark tile, the skeleton and its LOD budgets, wind colors, shell normals."""

import json
import math

import numpy as np
import pytest
from PIL import Image

from local_assets_engine.jobs import JobStore
from local_assets_engine.presets import PresetError, load_presets
from local_assets_engine.recipes import tree as tree_recipe
from local_assets_engine.tools import terrain_tile, tree_build, tree_textures


def leaf_sheet(size=256):
    """Three green leaves (stalks down), a brown twig, a strip along the left edge, and padding streaks."""
    alpha = np.zeros((size, size), dtype=np.float32)
    color = np.full((size, size, 3), 0.5, dtype=np.float32)
    rows, cols = np.mgrid[0:size, 0:size]
    for cx in (70, 130, 190):
        blade = ((cols - cx) / 18.0) ** 2 + ((rows - 110) / 45.0) ** 2 <= 1
        stalk = (np.abs(cols - cx) <= 1) & (rows >= 150) & (rows <= 170)
        alpha[blade | stalk] = 1
        color[blade | stalk] = (0.25, 0.45, 0.1)
    twig = (rows >= 210) & (rows <= 222) & (cols >= 60) & (cols <= 120)
    alpha[twig] = 1
    color[twig] = (0.35, 0.22, 0.12)
    strip = cols <= 12
    alpha[strip] = 1
    color[strip] = (0.3, 0.25, 0.2)
    # 패딩 줄: 잎을 잇는 가는 선(열림으로 끊겨야 한다).
    alpha[108:111, 70:190] = 1
    return alpha, terrain_tile.srgb_to_linear(color)


def test_elements_are_the_leaves_with_their_stalks_down_and_padding_cut():
    alpha, color = leaf_sheet()
    found, cleaned = tree_textures.elements(alpha, color, "down", open_px=2)
    assert len(found) == 3  # 가장자리 띠와 갈색 가지는 잎이 아니다
    for element in found:
        assert element["base"][1] > element["tip"][1]  # 잎자루 쪽(아래)이 붙는 곳
        assert element["lengthPx"] > element["widthPx"] * 1.8
    assert cleaned[109, 100] == 0  # 잎 사이 줄무늬는 렌더에 쓰는 알파에서 빠진다
    assert cleaned[110, 70] == 1 and cleaned[0:20, 5].max() == 0


def test_patch_offsets_keep_square_scans_on_the_old_random_stream_and_rectangles_inside():
    a = terrain_tile.patch_offsets(np.random.default_rng(4), 200, 40, 50)
    b = 4 + np.random.default_rng(4).integers(0, 200 - 40 - 8 + 1, size=(50, 2))
    assert np.array_equal(a, b)
    rect = terrain_tile.patch_offsets(np.random.default_rng(4), (300, 120), 40, 500)
    assert rect[:, 0].max() <= 300 - 40 - 4 and rect[:, 1].max() <= 120 - 40 - 4 and rect.min() >= 4


def test_bark_tile_keeps_the_scans_real_size_and_wraps(tmp_path):
    size = 128
    rows, cols = np.mgrid[0:size * 2, 0:size]
    # 세로 골(길이 방향)이 있는 1×2m 껍질: 가로·세로 모두 주기.
    height = 0.5 + 0.4 * np.sin(cols / size * 2 * math.pi * 6) * (0.7 + 0.3 * np.cos(rows / (size * 2) * 2 * math.pi * 3))
    gy, gx = np.gradient(height * 4)
    normal = np.stack([-gx, gy, np.ones_like(height)], axis=-1)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    folder = tmp_path / "bark"
    folder.mkdir()
    maps = {"diff": np.stack([0.3 + 0.2 * height] * 3, axis=-1), "nor_gl": normal * 0.5 + 0.5, "rough": np.full_like(height, 0.8),
            "ao": np.full_like(height, 0.9), "disp": height}
    for name, array in maps.items():
        Image.fromarray(np.clip(array * 255, 0, 255).astype(np.uint8)).save(folder / f"{name}.jpg", quality=97)
    spec = {"size": 64, "flatten": 0.5, "flattenMeters": 0.25, "grade": {},
            "scan": {"id": "bark", "dir": str(folder), "sizeMeters": 1.0, "dimensionsMeters": [1.0, 2.0]}}
    out, stats = tree_textures.bark_maps(spec, lambda *_: None)
    assert out["color"].shape == (64, 64, 3)
    assert stats["tileMeters"] == [1.0, 2.0] and stats["pxPerMeter"] == [64.0, 32.0]
    assert terrain_tile.wrap_seam_ratio(terrain_tile.luminance(out["color"])) < 1.4


def test_tube_wraps_whole_bark_tiles_and_counts_triangles():
    data = tree_build.MeshData()
    points = np.array([[0, 0, 0], [0, 0, 1.0], [0, 0.1, 2.0]])
    tree_build.tube(data, points, np.array([0.35, 0.3, 0.2]), 8, tile=(1.0, 2.0),
                    wind=lambda i: (i / 2, 0, 0, 0))
    assert data.triangles() == 2 * 8 * 2 + 8
    uvs = data.uvs[0]
    # 둘레 2π·r ≈ 2.0m → 1m 껍질 두 장이 이음 없이 감긴다.
    assert uvs[:, 0].max() == pytest.approx(2.0)
    assert uvs[9, 1] == pytest.approx(0.5)  # 둘째 고리: 1m 올라가 2m 껍질의 절반
    verts = data.verts[0][:9]
    assert np.allclose(np.linalg.norm(verts[:, :2], axis=1), 0.35)


def test_wind_colors_follow_the_contract():
    assert tree_build.wind_color(-0.4, 13.0, 0.0, 0.0, 0.0) == (0.0, 0.0, 0.0, 0.0)
    r, g, b, a = tree_build.wind_color(13.0, 13.0, 0.6, 1.0, 0.3)
    assert (r, g, b, a) == (1.0, 0.6, 1.0, 0.3)
    assert tree_build.wind_color(6.5, 13.0, 0, 0, 0)[0] == pytest.approx(0.5 ** 1.5)


def card(base, up, normal, g=0.5, phase=0.25):
    return tree_build.Card(np.array(base, float), tree_build.unit(np.array(up, float)), tree_build.unit(np.array(normal, float)),
                           1.0, 1.0, "twig0", g, phase)


def test_shell_normals_point_out_of_the_crown_and_cards_carry_wind():
    rng = np.random.default_rng(1)
    spec = {"form": "broadleaf", "heightMeters": 12.0, "crown": {"base": 4.0, "radius": 4.0}}
    env = tree_build.Envelope(spec)
    cards = []
    for _ in range(600):
        d = tree_build.unit(rng.normal(size=3))
        cards.append(card(np.array([0, 0, 8.0]) + d * rng.uniform(2.5, 3.8), d, rng.normal(size=3)))
    shell = tree_build.Shell(cards, env)
    probes = np.array([[3.5, 0, 8.0], [0, -3.5, 8.0], [0, 0, 11.5], [2.5, 2.5, 6.0]])
    normals = shell(probes)
    outward = tree_build.unit(probes - np.array([0, 0, 8.0]))
    alignment = np.sum(normals * outward, axis=1)
    assert (alignment > 0.6).all() and alignment.mean() > 0.85
    mesh = tree_build.card_mesh(cards[:5], 12.0, shell)
    colors = mesh.colors[0]
    assert mesh.triangles() == 10
    assert set(np.round(colors[:, 1], 3)) == {0.5} and set(np.round(colors[:, 3], 3)) == {0.25}
    assert list(colors[:4, 2]) == [0, 0, 1, 1]  # 붙는 쪽 0 → 자유 끝 1


@pytest.mark.parametrize("preset_id", ["temperate-oak", "temperate-birch", "temperate-fir"])
def test_species_skeleton_matches_size_and_budget(preset_id):
    presets = load_presets()
    preset = tree_recipe.find_tree_preset(presets, preset_id)
    spec = {**preset["build"], "name": preset["name"], "seed": preset["seed"]}
    branches = tree_build.skeleton(spec, np.random.default_rng(spec["seed"] + 101))
    trunk = branches[0]
    assert trunk.points[0, 2] == pytest.approx(-tree_build.UNDERGROUND)
    dbh = 2 * tree_build.trunk_radius(spec, np.array([1.3]), spec["heightMeters"])[0]
    assert dbh == pytest.approx(spec["dbhMeters"], rel=0.03)
    top = max(b.points[:, 2].max() for b in branches)
    assert spec["heightMeters"] * 0.85 < top < spec["heightMeters"] * 1.03
    budget = presets["trees"]["budget"]
    wood0 = tree_build.wood(branches, spec, 0, (1.0, 2.0)).triangles()
    assert budget["wood"][0] <= wood0 <= budget["wood"][1]
    assert tree_build.wood(branches, spec, 2, (1.0, 2.0)).triangles() < 0.15 * spec["triangles"]
    # 1차 가지의 위상은 그 가지에 달린 것 모두가 같이 쓴다.
    for branch in branches:
        if branch.level > 1:
            ancestor = branch
            while ancestor.level > 1:
                ancestor = ancestor.parent
            assert branch.phase == ancestor.phase
            assert 0 < branch.g0 < 1  # 붙는 자리의 부모 G에서 이어 간다
        elif branch.level == 1:
            assert branch.g0 == 0


def test_tree_recipe_scales_the_species_and_checks_names(tmp_path, monkeypatch):
    presets = load_presets()
    store = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(tree_recipe, "missing", lambda _presets, _scans=None: [])
    monkeypatch.setattr(tree_recipe, "missing_files", lambda _source: [])
    params, title = tree_recipe.prepare({"preset": "temperate-oak", "heightMeters": 6.5}, presets, store)
    preset = tree_recipe.find_tree_preset(presets, "temperate-oak")
    assert params["build"]["heightMeters"] == 6.5
    assert params["build"]["crown"]["radius"] == pytest.approx(preset["build"]["crown"]["radius"] / 2)
    assert params["build"]["trunk"]["fork"] == pytest.approx(preset["build"]["trunk"]["fork"] / 2)
    assert params["sources"]["bark"]["license"] == "CC0" and params["sources"]["leaves"]["page"].startswith("https://polyhaven.com/a/")
    assert params["textures"]["bark"]["scan"]["dimensionsMeters"] == [1.0, 2.0]
    json.dumps(params)
    with pytest.raises(PresetError):
        tree_recipe.prepare({"preset": "temperate-oak", "name": "Oak!"}, presets, store)
    with pytest.raises(PresetError):
        tree_recipe.prepare({"preset": "palm"}, presets, store)
    monkeypatch.setattr(tree_recipe, "missing", lambda _presets, _scans=None: ["jolcham_oak_bark_01/diff"])
    with pytest.raises(PresetError, match="materials fetch"):
        tree_recipe.prepare({"preset": "temperate-oak"}, presets, store)
