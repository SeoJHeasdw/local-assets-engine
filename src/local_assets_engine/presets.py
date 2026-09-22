"""Presets: prompt templates and defaults shared by the UI, CLI and recipes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .paths import PRESETS_PATH

KINDS = ("2d", "3d", "video")
STANDIN_SHAPES = ("box", "cylinder", "sphere")
STANDIN_AXES = ("x", "y", "z")
CAMERA_PARTS = ("shot", "angle", "move")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


class PresetError(ValueError):
    """Invalid preset file or a request that no preset can satisfy."""


def _check_standin(standin: dict[str, Any]) -> None:
    standin_id = standin["id"]
    parts = standin.get("parts") or []
    try:
        size = [float(value) for value in standin["size"]]
        boxes = [([float(value) for value in part["at"]], [float(value) for value in part["size"]]) for part in parts]
    except (KeyError, TypeError, ValueError) as error:
        raise PresetError(f"{standin_id}: size와 부품의 at·size는 숫자 세 개여야 합니다.") from error
    if not parts or len(size) != 3 or any(len(at) != 3 or len(extent) != 3 for at, extent in boxes):
        raise PresetError(f"{standin_id}: size와 부품의 at·size는 숫자 세 개여야 합니다.")
    if min(size) <= 0 or any(min(extent) <= 0 for _, extent in boxes):
        raise PresetError(f"{standin_id}: 치수는 0보다 커야 합니다.")
    for part in parts:
        if part.get("shape") not in STANDIN_SHAPES or part.get("axis", "z") not in STANDIN_AXES:
            raise PresetError(f"{standin_id}: 부품 도형은 {STANDIN_SHAPES}, 축은 {STANDIN_AXES} 중 하나여야 합니다.")
    # 앱 지도는 size로 카메라 자리를 계산하고 엔진은 만든 도형의 경계를 잰다. 둘이 어긋나면
    # 지도에서 잡은 카메라와 렌더 결과가 달라진다.
    spans = [max(at[axis] + extent[axis] / 2 for at, extent in boxes)
             - min(at[axis] - extent[axis] / 2 for at, extent in boxes) for axis in range(3)]
    if any(abs(span - declared) > 1e-3 for span, declared in zip(spans, size)):
        raise PresetError(f"{standin_id}: 부품이 차지하는 범위 {[round(span, 3) for span in spans]}가 size {size}와 다릅니다.")


def _check_video(data: dict[str, Any]) -> None:
    models = video_models(data)
    if len({model["id"] for model in models}) != len(models):
        raise PresetError("영상 모델 id가 중복됩니다.")
    for model in models:
        # 영상 가중치는 수십 GB라 저장소의 main이 바뀌면 다른 모델을 받게 된다. 커밋으로만 고정한다.
        if not COMMIT.match(str(model.get("revision") or "")):
            raise PresetError(f"{model['id']}: revision은 40자리 커밋 해시여야 합니다.")
        width, height, frames = int(model["width"]), int(model["height"]), int(model["frames"])
        if width % 32 or height % 32 or width * height > int(model["maxPixels"]):
            raise PresetError(f"{model['id']}: 기본 크기는 32의 배수이고 maxPixels 이하여야 합니다.")
        if (frames - 1) % 4 or frames > int(model["maxFrames"]):
            raise PresetError(f"{model['id']}: 프레임 수는 4의 배수 + 1이고 maxFrames 이하여야 합니다.")
    camera = data.get("video", {}).get("camera", {})
    for part in CAMERA_PARTS:
        options = camera.get(part, [])
        if len({option["id"] for option in options}) != len(options) or any(not option.get("text") for option in options):
            raise PresetError(f"카메라 {part} 선택지의 id가 중복되거나 문장이 비었습니다.")
    if default := data.get("video", {}).get("defaultPreset"):
        find_preset(data, default, kind="video")


def load_presets(path: Path = PRESETS_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text("utf-8"))
    models = image_models(data)
    if len({model["id"] for model in models}) != len(models):
        raise PresetError("이미지 모델 id가 중복됩니다.")
    _check_video(data)
    categories = data.get("imageCategories", [])
    category_ids = {category["id"] for category in categories}
    if len(category_ids) != len(categories):
        raise PresetError("이미지 카테고리 id가 중복됩니다.")
    seen: set[str] = set()
    for preset in data.get("presets", []):
        preset_id = preset.get("id")
        if not preset_id or preset_id in seen:
            raise PresetError(f"프리셋 id가 비었거나 중복됩니다: {preset_id!r}")
        seen.add(preset_id)
        if preset.get("kind") not in KINDS:
            raise PresetError(f"{preset_id}: kind는 {KINDS} 중 하나여야 합니다.")
        if preset.get("category") and preset["category"] not in category_ids:
            raise PresetError(f"{preset_id}: 알 수 없는 이미지 카테고리입니다.")
        if preset.get("imageModel"):
            find_image_model(data, preset["imageModel"])
        if "{subject}" not in preset.get("prompt", ""):
            raise PresetError(f"{preset_id}: prompt에 {{subject}} 자리가 없습니다.")
        if preset.get("pixelate") and not preset.get("removeBackground"):
            raise PresetError(f"{preset_id}: 픽셀화는 배경 제거 뒤에만 쓸 수 있습니다.")
    seen.clear()
    for preset in data.get("previz", {}).get("shotPresets", []):
        preset_id = preset.get("id")
        if not preset_id or preset_id in seen:
            raise PresetError(f"샷 프리셋 id가 비었거나 중복됩니다: {preset_id!r}")
        seen.add(preset_id)
        shots = preset.get("shots") or []
        if not shots:
            raise PresetError(f"{preset_id}: 샷이 비어 있습니다.")
        # 샷 id는 결과 폴더 이름이 된다.
        if len({shot.get("id") for shot in shots}) != len(shots):
            raise PresetError(f"{preset_id}: 샷 id가 중복됩니다.")
        for shot in shots:
            if not shot.get("id") or float(shot.get("seconds", 0)) <= 0:
                raise PresetError(f"{preset_id}: 샷에는 id와 0보다 큰 seconds가 필요합니다.")
    seen.clear()
    for standin in data.get("previz", {}).get("standins", []):
        standin_id = standin.get("id")
        if not standin_id or standin_id in seen:
            raise PresetError(f"대역 id가 비었거나 중복됩니다: {standin_id!r}")
        seen.add(standin_id)
        _check_standin(standin)
    return data


def image_models(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The existing default plus explicitly configured alternatives."""
    default = [data["imageModel"]] if data.get("imageModel") else []
    return default + data.get("imageModels", [])


def find_image_model(data: dict[str, Any], model_id: str | None = None) -> dict[str, Any]:
    model_id = model_id or data["imageModel"]["id"]
    for model in image_models(data):
        if model["id"] == model_id:
            return model
    raise PresetError(f"알 수 없는 이미지 모델입니다: {model_id}")


def video_models(data: dict[str, Any]) -> list[dict[str, Any]]:
    default = [data["videoModel"]] if data.get("videoModel") else []
    return default + data.get("videoModels", [])


def find_video_model(data: dict[str, Any], model_id: str | None = None) -> dict[str, Any]:
    if not data.get("videoModel"):
        raise PresetError("설정에 영상 모델이 없습니다.")
    model_id = model_id or data["videoModel"]["id"]
    for model in video_models(data):
        if model["id"] == model_id:
            return model
    raise PresetError(f"알 수 없는 영상 모델입니다: {model_id}")


def find_preset(data: dict[str, Any], preset_id: str, *, kind: str | None = None) -> dict[str, Any]:
    for preset in data.get("presets", []):
        if preset["id"] == preset_id:
            if kind and preset["kind"] != kind:
                raise PresetError(f"{preset_id}는 {kind} 프리셋이 아닙니다.")
            return preset
    raise PresetError(f"알 수 없는 프리셋입니다: {preset_id}")


def find_shot_preset(data: dict[str, Any], preset_id: str) -> dict[str, Any]:
    for preset in data.get("previz", {}).get("shotPresets", []):
        if preset["id"] == preset_id:
            return preset
    raise PresetError(f"알 수 없는 샷 프리셋입니다: {preset_id}")


def find_standin(data: dict[str, Any], standin_id: str) -> dict[str, Any]:
    for standin in data.get("previz", {}).get("standins", []):
        if standin["id"] == standin_id:
            return standin
    raise PresetError(f"알 수 없는 대역입니다: {standin_id}")


def build_prompt(preset: dict[str, Any], subject: str, style: str = "") -> str:
    subject = " ".join(str(subject or "").split())
    if not subject:
        raise PresetError("무엇을 만들지 적어 주세요.")
    prompt = preset["prompt"].replace("{subject}", subject)
    style = " ".join(str(style or "").split())
    return f"{prompt}, {style}" if style else prompt
