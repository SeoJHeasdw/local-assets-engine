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


def test_description_is_preserved_as_a_note_without_changing_the_render_plan(tmp_path):
    plain, _ = prepare({}, tmp_path)
    described, _ = prepare({"description": "  두 사람이 마주 본다.  "}, tmp_path)
    assert described["description"] == "두 사람이 마주 본다."
    for key in ("shots", "renderer", "width", "height", "fps", "samples", "clay"):
        assert described[key] == plain[key]
    for value in (None, {"prompt": "사람"}, "가" * 2001):
        with pytest.raises(PresetError, match="설명"):
            prepare({"description": value}, tmp_path)


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


def spans(parts):
    return [max(part["at"][axis] + part["size"][axis] / 2 for part in parts)
            - min(part["at"][axis] - part["size"][axis] / 2 for part in parts) for axis in range(3)]


def test_stand_ins_block_out_a_scene_before_any_asset_is_generated(tmp_path):
    store = JobStore(tmp_path / "jobs")
    normalized, title = PREVIZ.prepare(
        {"assets": [{"standin": "person", "yaw": 30}, {"standin": "wall", "position": [0, 3, 0]}]},
        load_presets(), store,
    )
    assert title == "프리비즈 · 게임 트레일러 · 사람 대역"
    person, wall = normalized["assets"]
    assert (person["id"], person["standin"], person["file"], person["source"]) == ("hero", "person", None, None)
    assert (person["size"], person["yaw"], wall["position"]) == ([0.55, 0.29, 1.75], 30.0, [0.0, 3.0, 0.0])
    assert spans(person["parts"]) == pytest.approx(person["size"], abs=1e-3)


def test_a_resized_stand_in_stretches_its_parts_to_the_new_size(tmp_path):
    # 지도는 size로 카메라를 계산하고 엔진은 만든 도형의 경계를 잰다. 늘인 뒤에도 둘이 같아야 한다.
    store, job_id = mesh_job(tmp_path)
    normalized, _ = PREVIZ.prepare({"assets": [
        {"source": {"jobId": job_id, "assetId": "a02"}},
        {"standin": "car", "size": [2.5, 8, "3.2"]},
    ]}, load_presets(), store)
    chest, truck = normalized["assets"]
    assert "parts" not in chest and chest["file"].endswith("asset.glb")
    assert truck["size"] == [2.5, 8.0, 3.2]
    assert spans(truck["parts"]) == pytest.approx([2.5, 8.0, 3.2], abs=1e-3)


def test_unknown_or_malformed_stand_ins_are_rejected(tmp_path):
    store = JobStore(tmp_path / "jobs")
    for entry, match in (
        ({"standin": "dragon"}, "대역"), ({"standin": "wall", "size": [4, 0.2]}, "size"),
        ({"standin": "wall", "size": [0, 0.2, 2.5]}, "size"), ("person", "형식"),
    ):
        with pytest.raises(PresetError, match=match):
            PREVIZ.prepare({"assets": [entry]}, load_presets(), store)


def test_a_stand_in_whose_parts_do_not_fill_its_size_is_caught_when_the_config_loads(tmp_path):
    broken = tmp_path / "presets.json"
    crate = {"id": "crate", "size": [1, 1, 1], "parts": [{"shape": "box", "at": [0, 0, 0.5], "size": [1, 1, 1]}]}
    base = {"presets": [], "previz": {"shotPresets": [], "standins": [crate]}}
    broken.write_text(json.dumps(base), "utf-8")
    assert load_presets(broken)["previz"]["standins"][0]["id"] == "crate"
    for change, match in (({"size": [1, 1, 2]}, "범위"), ({"parts": [{**crate["parts"][0], "shape": "cone"}]}, "도형"),
                          ({"parts": [{**crate["parts"][0], "at": [0, 0]}]}, "숫자")):
        base["previz"]["standins"] = [{**crate, **change}]
        broken.write_text(json.dumps(base), "utf-8")
        with pytest.raises(PresetError, match=match):
            load_presets(broken)


def test_cuts_rearranged_in_the_app_replace_the_preset(tmp_path):
    shots = [
        {"id": "s02", "label": "주인공 공개", "move": "orbit", "focus": "hero", "lens": 50, "seconds": 3,
         "framing": {"distance": 1.7, "azimuth": 20, "azimuthEnd": 80, "height": 0.8, "targetHeight": 0.55}},
        {"id": "s01", "focus": "scene", "lens": "28", "seconds": "2.5", "framing": {"distance": 3}},
    ]
    normalized, title = prepare({"shots": shots}, tmp_path)
    assert title.startswith("프리비즈 · 게임 트레일러 편집")
    assert normalized["edited"] is True
    assert [(shot["id"], shot["order"], shot["focus"]) for shot in normalized["shots"]] == [
        ("s02", 1, "hero"), ("s01", 2, "scene"),
    ]
    assert normalized["shots"][0]["framing"]["azimuthEnd"] == 80.0
    assert (normalized["shots"][1]["lens"], normalized["shots"][1]["seconds"]) == (28.0, 2.5)


def test_edited_cuts_cannot_escape_the_shot_folder_or_break_ranges(tmp_path):
    base = {"focus": "hero", "lens": 35, "seconds": 2, "framing": {"distance": 1.5}}
    for bad_id in ("../../etc", "s 01", "", "a" * 40):
        with pytest.raises(PresetError, match="컷 id"):
            prepare({"shots": [{**base, "id": bad_id}]}, tmp_path)
    with pytest.raises(PresetError, match="겹치지"):
        prepare({"shots": [{**base, "id": "s01"}, {**base, "id": "s01"}]}, tmp_path)
    with pytest.raises(PresetError, match="distance"):
        prepare({"shots": [{**base, "id": "s01", "framing": {"distance": 0}}]}, tmp_path)
    with pytest.raises(PresetError, match="focus"):
        prepare({"shots": [{**base, "id": "s01", "focus": "nobody"}]}, tmp_path)


def test_shot_presets_in_the_repository_are_usable(tmp_path):
    presets = load_presets()
    trailer = presets["previz"]["shotPresets"][0]
    assert trailer["id"] == "game-trailer" and len(trailer["shots"]) >= 4
    # 컷마다 길이·렌즈·프레이밍이 있어야 카메라 값으로 풀린다.
    for shot in trailer["shots"]:
        assert shot["seconds"] > 0 and shot["lens"] > 0 and shot["framing"]["distance"] > 0
    assert {standin["id"] for standin in presets["previz"]["standins"]} >= {"person", "car", "wall", "building"}


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
