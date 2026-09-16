import numpy as np
import pytest
from PIL import Image

from local_assets_engine import imaging
from local_assets_engine.presets import PresetError, build_prompt, find_preset, load_presets
from local_assets_engine.recipes.image import prepare_image_params
from local_assets_engine.recipes.mesh import prepare_mesh_params


def test_repository_presets_are_valid():
    data = load_presets()
    assert {preset["kind"] for preset in data["presets"]} == {"2d", "3d"}
    assert find_preset(data, "prop-3d")["kind"] == "3d"
    with pytest.raises(PresetError):
        find_preset(data, "item-icon", kind="3d")


def test_build_prompt_collapses_whitespace_and_appends_style():
    preset = {"prompt": "{subject}, icon"}
    assert build_prompt(preset, "  red   potion ", " watercolor ") == "red potion, icon, watercolor"
    with pytest.raises(PresetError):
        build_prompt(preset, "   ")


def test_load_presets_rejects_template_without_subject(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text('{"presets": [{"id": "x", "kind": "2d", "prompt": "no slot"}]}')
    with pytest.raises(PresetError):
        load_presets(path)


def test_image_params_use_consecutive_seeds_and_preset_outputs():
    params = prepare_image_params({"subject": "fire sword", "preset": "pixel-art", "count": 3, "seed": 7}, load_presets())
    assert params["seeds"] == [7, 8, 9]
    assert params["pixelate"] == {"size": 64, "colors": 24}
    no_cutout = prepare_image_params({"subject": "x", "preset": "pixel-art", "removeBackground": "false"}, load_presets())
    assert no_cutout["pixelate"] is None and no_cutout["canvas"] is None
    with pytest.raises(PresetError):
        prepare_image_params({"subject": "x", "count": 99}, load_presets())


def test_mesh_params_validate_choices():
    params = prepare_mesh_params({"pipelineType": "1024", "textureSize": "2048", "meshSeed": 5}, load_presets())
    assert params == {"pipelineType": "1024", "textureSize": 2048, "targetFaces": 1000000,
                      "sizeMeters": 1.0, "meshSeed": 5, "audit": False,
                      "gameFaces": 0, "gameTextureSize": 2048}
    with pytest.raises(PresetError):
        prepare_mesh_params({"pipelineType": "4096"}, load_presets())


def _block(size=200, box=(50, 60, 150, 140)):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    image.paste((200, 30, 30, 255), box)
    return image


def test_fit_to_canvas_centers_object_inside_padding():
    fitted = imaging.fit_to_canvas(_block(), 100, 100, padding=0.1)
    assert fitted.size == (100, 100)
    assert imaging.alpha_bbox(fitted) == (10, 18, 90, 82)


def test_fit_to_canvas_rejects_empty_cutout():
    with pytest.raises(ValueError):
        imaging.fit_to_canvas(Image.new("RGBA", (10, 10)), 8, 8)


def test_pixelate_limits_size_palette_and_hardens_alpha():
    image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    for x in range(16, 112):
        for y in range(16, 112):
            image.putpixel((x, y), (x * 2, y * 2, 128, 255))
    small = imaging.pixelate(image, 32, 8)
    assert small.size == (32, 32)
    pixels = np.asarray(small)
    assert set(np.unique(pixels[:, :, 3]).tolist()) <= {0, 255}
    assert len({tuple(color) for color in pixels[pixels[:, :, 3] == 255][:, :3].tolist()}) <= 8


def test_cutout_checks_and_transparency_detection():
    image = _block()
    assert imaging.has_transparency(image)
    assert not imaging.has_transparency(Image.new("RGB", (4, 4)))
    checks = imaging.cutout_checks(image)
    assert checks["objectFound"] and not checks["touchesEdge"]
    assert 0 < checks["coverage"] < 1
