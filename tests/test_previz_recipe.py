import json

import pytest

from local_assets_engine.jobs import JobStore
from local_assets_engine.presets import PresetError, load_presets
from local_assets_engine.recipes.previz import PREVIZ
from local_assets_engine.tools.previz_render import _interpolate, _pair


def mesh_job(tmp_path):
    store = JobStore(tmp_path / "jobs")
    job = store.create("text-to-3d", {"subject": "나무 상자"}, "3D 소품 · 나무 상자")
    glb = store.job_dir(job["id"]) / "mesh"
    glb.mkdir()
    (glb / "asset.glb").write_bytes(b"glb")
    store.update(job["id"], lambda record: record["assets"].append(
        {"id": "a02", "kind": "mesh", "role": "final", "file": "mesh/asset.glb", "review": "pending", "meta": {}},
    ))
    return store, job["id"]


def prepare(params, tmp_path, presets=None):
    store, job_id = mesh_job(tmp_path)
    if "assets" not in params and "path" not in params:
        params = {**params, "source": {"jobId": job_id, "assetId": "a02"}}
    return PREVIZ.prepare(params, presets or load_presets(), store)


def test_a_finished_mesh_becomes_the_hero_of_every_preset_shot(tmp_path):
    normalized, title = prepare({}, tmp_path)
    assert title == "프리비즈 · 게임 트레일러 · 나무 상자"
    assert normalized["hero"] == "hero" and normalized["assets"][0]["label"] == "나무 상자"
    # 프리셋은 "hero"라고만 쓰고, 어느 에셋이 주인공인지는 장면이 정한다.
    assert {shot["focus"] for shot in normalized["shots"]} == {"hero", "scene"}
    assert [shot["order"] for shot in normalized["shots"]] == list(range(1, len(normalized["shots"]) + 1))


def test_defaults_come_from_the_config_and_can_be_overridden(tmp_path):
    normalized, _ = prepare({}, tmp_path)
    assert (normalized["renderer"], normalized["clay"], normalized["animatic"]) == ("eevee", True, True)
    assert normalized["look"]["clay"] is True
    other, _ = prepare({"renderer": "workbench", "clay": False, "fps": 24, "aux": "all"}, tmp_path)
    assert (other["renderer"], other["clay"], other["fps"], other["aux"]) == ("workbench", False, 24, "all")
    assert other["look"]["clay"] is False


def test_placement_keeps_meters_and_degrees_as_given(tmp_path):
    store, job_id = mesh_job(tmp_path)
    normalized, _ = PREVIZ.prepare(
        {"assets": [{"source": {"jobId": job_id, "assetId": "a02"}, "position": [1, -2, 0], "yaw": 90, "scale": 2}]},
        load_presets(), store,
    )
    placed = normalized["assets"][0]
    assert (placed["position"], placed["yaw"], placed["scale"]) == ([1.0, -2.0, 0.0], 90.0, 2.0)


def test_an_unknown_renderer_or_missing_asset_is_rejected(tmp_path):
    with pytest.raises(PresetError, match="renderer"):
        prepare({"renderer": "octane"}, tmp_path)
    store, _ = mesh_job(tmp_path)
    with pytest.raises(PresetError, match="에셋"):
        PREVIZ.prepare({}, load_presets(), store)
    with pytest.raises(PresetError, match="에셋"):
        PREVIZ.prepare({"source": {"jobId": "20260101-000000-abcd", "assetId": "a01"}}, load_presets(), store)


def test_only_absolute_mesh_files_are_accepted_from_disk(tmp_path):
    store, _ = mesh_job(tmp_path)
    for path in ("mesh/asset.glb", str(tmp_path / "missing.glb"), str(tmp_path)):
        with pytest.raises(PresetError, match="GLB"):
            PREVIZ.prepare({"path": path}, load_presets(), store)
    outside = tmp_path / "scene.glb"
    outside.write_bytes(b"glb")
    normalized, title = PREVIZ.prepare({"path": str(outside)}, load_presets(), store)
    assert normalized["assets"][0]["file"] == str(outside) and "scene" in title


def test_shot_presets_in_the_repository_are_usable(tmp_path):
    presets = load_presets()
    trailer = presets["previz"]["shotPresets"][0]
    assert trailer["id"] == "game-trailer" and len(trailer["shots"]) >= 4
    # 컷마다 길이·렌즈·프레이밍이 있어야 카메라 값으로 풀린다.
    for shot in trailer["shots"]:
        assert shot["seconds"] > 0 and shot["lens"] > 0 and shot["framing"]["distance"] > 0


def test_a_broken_shot_preset_is_caught_when_the_config_loads(tmp_path):
    broken = tmp_path / "presets.json"
    base = {"presets": [], "previz": {"shotPresets": [{"id": "x", "shots": [
        {"id": "s01", "seconds": 1}, {"id": "s01", "seconds": 1},
    ]}]}}
    broken.write_text(json.dumps(base), "utf-8")
    with pytest.raises(PresetError, match="중복"):
        load_presets(broken)
    base["previz"]["shotPresets"][0]["shots"][1]["id"] = "s02"
    base["previz"]["shotPresets"][0]["shots"][1]["seconds"] = 0
    broken.write_text(json.dumps(base), "utf-8")
    with pytest.raises(PresetError, match="seconds"):
        load_presets(broken)


def test_easing_keeps_the_ends_exact_and_eases_the_middle():
    assert (_interpolate(2.0, 4.0, 0.0, "inout"), _interpolate(2.0, 4.0, 1.0, "inout")) == (2.0, 4.0)
    assert _interpolate(0.0, 1.0, 0.5, "inout") == pytest.approx(0.5)
    assert _interpolate(0.0, 1.0, 0.25, "inout") < _interpolate(0.0, 1.0, 0.25, "linear")


def test_a_framing_value_without_an_end_holds_still():
    assert _pair({"distance": 2.0}, "distance", 1.0) == (2.0, 2.0)
    assert _pair({"distance": 2.0, "distanceEnd": 1.2}, "distance", 1.0) == (2.0, 1.2)
    assert _pair({}, "height", 0.6) == (0.6, 0.6)
