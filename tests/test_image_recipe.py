import json
from pathlib import Path

import pytest

from local_assets_engine.presets import load_presets
from local_assets_engine.recipes.image import build_image_command, match_generated_files


def test_model_choice_is_validated_and_snapshotted():
    from local_assets_engine.presets import PresetError
    from local_assets_engine.recipes.image import prepare_image_params
    presets = load_presets()
    params = prepare_image_params({"subject": "a traveler", "preset": "anime-character", "seed": 7,
                                   "imageModel": "z-image-turbo", "imageModelConfig": {"command": "bad"}}, presets)
    assert params["category"] == "anime" and not params["removeBackground"]
    assert params["canvas"] is None and params["pixelate"] is None
    presets["imageModels"][0]["steps"] = 100
    assert params["imageModelConfig"]["steps"] == 9
    args = build_image_command(params["imageModelConfig"], params, Path("out.png"))
    assert str(args[0]).endswith("mflux-generate-z-image-turbo")
    assert args[args.index("--model") + 1] == "z-image-turbo"
    assert args[args.index("--steps") + 1] == "9"
    with pytest.raises(PresetError, match="이미지 모델"):
        prepare_image_params({"subject": "x", "imageModel": "unknown"}, presets)


def test_category_presets_keep_background_and_existing_defaults():
    from local_assets_engine.recipes.image import prepare_image_params
    presets = load_presets()
    for preset in presets["presets"]:
        if preset.get("category") not in ("photo", "anime"):
            continue
        params = prepare_image_params({"subject": "a traveler", "preset": preset["id"]}, presets)
        assert not params["removeBackground"] and params["imageModel"] == "flux2-klein-4b"
    legacy = prepare_image_params({"subject": "a sword"}, presets)
    assert legacy["preset"] == "item-icon" and legacy["removeBackground"]
    assert legacy["canvas"]["width"] == 512


def test_image_command_carries_every_seed_and_memory_option():
    model = {"command": "mflux-generate-z-image-turbo", "quantize": 8, "mlxCacheLimitGb": 8, "lowRam": True}
    params = {"prompt": "a sword", "width": 1024, "height": 768, "seeds": [7, 8]}
    args = [str(part) for part in build_image_command(model, params, Path("/tmp/out/candidate.png"))]
    assert args[0].endswith("mflux-generate-z-image-turbo")
    assert args[args.index("--seed") + 1:args.index("--seed") + 3] == ["7", "8"]
    assert args[args.index("--width") + 1] == "1024" and args[args.index("--height") + 1] == "768"
    assert args[args.index("--quantize") + 1] == "8"
    assert args[args.index("--mlx-cache-limit-gb") + 1] == "8"
    assert "--low-ram" in args and "--metadata" in args


def test_image_command_omits_options_the_model_does_not_set():
    args = [str(part) for part in build_image_command(
        {"command": "mflux-generate"}, {"prompt": "x", "width": 512, "height": 512, "seeds": [1]}, Path("out.png"),
    )]
    assert not {"--quantize", "--mlx-cache-limit-gb", "--low-ram", "--steps"} & set(args)


def test_repository_image_model_is_configured_for_this_machine():
    model = load_presets()["imageModel"]
    assert model["quantize"] == 8 and model["mlxCacheLimitGb"]


def write_candidate(directory, name, seed=None):
    (directory / f"{name}.png").write_bytes(b"png")
    if seed is not None:
        (directory / f"{name}.metadata.json").write_text(json.dumps({"seed": seed}), "utf-8")


def test_metadata_decides_which_file_belongs_to_a_seed(tmp_path):
    # 파일 이름이 시드를 말해 주지 않아도 메타데이터로 짝짓는다.
    write_candidate(tmp_path, "candidate", seed=18)
    write_candidate(tmp_path, "candidate_1", seed=7)
    assert [path.name for path in match_generated_files(tmp_path, [7, 18])] == ["candidate_1.png", "candidate.png"]


def test_falls_back_to_the_seed_suffix_when_metadata_is_missing(tmp_path):
    write_candidate(tmp_path, "candidate_seed_7")
    write_candidate(tmp_path, "candidate_seed_18")
    assert [path.name for path in match_generated_files(tmp_path, [18, 7])] == ["candidate_seed_18.png", "candidate_seed_7.png"]


def test_broken_metadata_does_not_hide_a_usable_file(tmp_path):
    write_candidate(tmp_path, "candidate_seed_7")
    (tmp_path / "candidate_seed_7.metadata.json").write_text("{broken", "utf-8")
    assert [path.name for path in match_generated_files(tmp_path, [7])] == ["candidate_seed_7.png"]


def test_missing_seeds_are_named_in_the_error(tmp_path):
    write_candidate(tmp_path, "candidate_seed_7", seed=7)
    with pytest.raises(RuntimeError, match="시드 8"):
        match_generated_files(tmp_path, [7, 8])
