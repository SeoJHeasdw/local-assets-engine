"""JSON Schema of each recipe's request params, for ``GET /api/recipes``.

A schema states what the recipe's ``prepare`` checks, in canonical JSON types,
and is built from the same constants and preset lists. ``prepare`` stays the
authority: it also reads numbers and booleans written as strings, ignores keys
it does not know, and enforces what a schema cannot say (a file that must
exist, a limit that depends on another field — those are in ``description``).
``tests/test_recipe_schemas.py`` probes every bound and choice against
``prepare`` so the two cannot drift apart unnoticed.
"""

from __future__ import annotations

from typing import Any, Callable

from ..presets import CAMERA_PARTS, image_models, video_models
from ..uploads import ID_PATTERN as UPLOAD_ID
from . import building_kit, edit_asset, game_asset, image, previz, selftest, video
from .base import MAX_SEED

Schema = dict[str, Any]
COLOR = {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}


def _integer(low: int, high: int, default: int | None = None, description: str | None = None) -> Schema:
    return _described({"type": "integer", "minimum": low, "maximum": high}, default, description)


def _number(low: float, high: float, default: float | None = None, description: str | None = None) -> Schema:
    return _described({"type": "number", "minimum": low, "maximum": high}, default, description)


def _choice(values: list[Any], default: Any = None, description: str | None = None) -> Schema:
    return _described({"enum": list(values)}, default if default in values else None, description)


def _boolean(default: bool | None = None, description: str | None = None) -> Schema:
    return _described({"type": "boolean"}, default, description)


def _text(limit: int | None = None, description: str | None = None) -> Schema:
    return _described({"type": "string", **({"maxLength": limit} if limit else {})}, None, description)


def _described(schema: Schema, default: Any, description: str | None) -> Schema:
    if default is not None:
        schema["default"] = default
    if description:
        schema["description"] = description
    return schema


def _vector(size: int, low: float, high: float, description: str | None = None) -> Schema:
    return _described({"type": "array", "items": {"type": "number", "minimum": low, "maximum": high},
                       "minItems": size, "maxItems": size}, None, description)


def _object(properties: Schema, *, required: list[str] | None = None, **keywords: Any) -> Schema:
    return {"type": "object", "properties": properties, **({"required": required} if required else {}), **keywords}


def _source(description: str) -> Schema:
    return _object({"jobId": {"type": "string"}, "assetId": {"type": "string"}},
                   required=["jobId", "assetId"], description=description)


def _one_of_keys(*keys: str) -> list[Schema]:
    return [{"required": [key]} for key in keys]


def _image_source() -> Schema:
    return {
        "uploadId": {"type": "string", "pattern": UPLOAD_ID.pattern, "description": "POST /api/uploads가 준 id"},
        "source": _source("보관함의 이미지 에셋"),
        "imagePath": _text(description="PNG·JPG·WEBP 파일의 절대 경로"),
    }


def _image_inputs(presets: dict[str, Any], *, kind: str | None, default_preset: str, default_count: int,
                  max_count: int = image.MAX_CANDIDATES) -> Schema:
    preset_ids = [preset["id"] for preset in presets.get("presets", []) if kind is None or preset["kind"] == kind]
    return {
        "subject": {"type": "string", "pattern": r"\S", "description": "무엇을 만들지"},
        "preset": _choice(preset_ids, default_preset),
        "style": _text(description="프롬프트 끝에 붙는 스타일"),
        "imageModel": _choice([model["id"] for model in image_models(presets)],
                              description="비우면 프리셋의 모델, 없으면 기본 모델"),
        "count": _integer(1, image.MAX_CANDIDATES, default_count, "후보 장수. 시드는 시작 값부터 1씩 늘린다"),
        # 후보마다 다음 시드를 쓰므로 장수만큼 여유를 남긴 값까지만 약속한다.
        "seed": _integer(0, MAX_SEED - max_count, description="비우면 엔진이 무작위로 정해 기록한다"),
        "width": _integer(256, 2048, description="비우면 프리셋 크기. 16의 배수로 내린다"),
        "height": _integer(256, 2048, description="비우면 프리셋 크기. 16의 배수로 내린다"),
        "removeBackground": _boolean(description="비우면 프리셋 값"),
    }


def _mesh_outputs(presets: dict[str, Any]) -> Schema:
    mesh = presets["mesh"]
    defaults = mesh["defaults"]
    return {
        "textureSize": _choice(mesh["textureSizes"], defaults["textureSize"]),
        "targetFaces": _integer(0, 1_000_000, defaults["targetFaces"], "품질본 면 수. 0은 줄이지 않음"),
        "gameFaces": _integer(0, 1_000_000, defaults.get("gameFaces", 0), "0은 게임용 생략"),
        "gameTextureSize": _choice(mesh["textureSizes"], defaults.get("gameTextureSize", 2048)),
        "sizeMeters": _number(0.0, 1000.0, defaults["sizeMeters"], "가장 긴 변의 미터 크기. 0은 크기 유지"),
        "audit": _boolean(False),
    }


def _mesh_inputs(presets: dict[str, Any]) -> Schema:
    mesh = presets["mesh"]
    return {
        "pipelineType": _choice(mesh["pipelineTypes"], mesh["defaults"]["pipelineType"]),
        "meshSeed": _integer(0, MAX_SEED - 1, description="비우면 엔진이 무작위로 정해 기록한다"),
        **_mesh_outputs(presets),
    }


def _camera(presets: dict[str, Any]) -> Schema:
    options = presets.get("video", {}).get("camera", {})
    return _object({part: _choice([option["id"] for option in options.get(part, [])]) for part in CAMERA_PARTS},
                   additionalProperties=False, description="샷 크기·앵글·움직임. 비운 부분은 문장을 더하지 않는다")


def _video_inputs(presets: dict[str, Any]) -> Schema:
    models = video_models(presets)
    if not models:
        return {}
    default = models[0]
    size = f"32의 배수로 내린다. 가로×세로는 모델의 maxPixels({default['maxPixels']:,}) 이하"
    return {
        "videoModel": _choice([model["id"] for model in models], default["id"]),
        "width": _integer(256, 2048, description=f"비우면 {default['width']}(세로 원본은 {default['height']}). {size}"),
        "height": _integer(256, 2048, description=f"비우면 {default['height']}(세로 원본은 {default['width']}). {size}"),
        "frames": _integer(5, max(model["maxFrames"] for model in models), default["frames"],
                           "4의 배수+1로 내린다. 고른 모델의 maxFrames 이하"),
        "steps": _integer(1, 100, default["steps"]),
        "guidance": _number(1.0, 20.0, default["guidance"]),
        "negativePrompt": _text(video.MAX_TEXT, "비우면 모델의 공식 부정 프롬프트"),
    }


def _video_prompt(presets: dict[str, Any]) -> Schema:
    return {
        "motion": _text(video.MAX_TEXT, "무엇이 어떻게 움직이는지"),
        "camera": _camera(presets),
        "videoPrompt": _text(video.MAX_TEXT, "있으면 조립하지 않고 이 문장을 그대로 보낸다"),
    }


def _image(presets: dict[str, Any]) -> Schema:
    return _object(_image_inputs(presets, kind=None, default_preset=image.DEFAULT_PRESET, default_count=4),
                   required=["subject"])


def _text_to_3d(presets: dict[str, Any]) -> Schema:
    return _object({**_image_inputs(presets, kind="3d", default_preset="prop-3d", default_count=1),
                    **_mesh_inputs(presets)}, required=["subject"])


def _image_to_3d(presets: dict[str, Any]) -> Schema:
    return _object({**_image_source(), "name": _text(description="imagePath로 넣을 때의 제목"),
                    "removeBackground": _boolean(True, "알파가 없는 이미지의 배경을 먼저 지운다"),
                    **_mesh_inputs(presets)}, anyOf=_one_of_keys("uploadId", "source", "imagePath"))


def _repair_mesh(_presets: dict[str, Any]) -> Schema:
    return _object({"source": _source("완료된 이전 KDTree 메시. 시드·해상도·면 수·크기는 원본 값을 쓴다")},
                   required=["source"])


def _refine_mesh(presets: dict[str, Any]) -> Schema:
    outputs = _mesh_outputs(presets)
    outputs["sizeMeters"].pop("default")
    outputs["sizeMeters"]["description"] += ". 비우면 원본 값"
    return _object({"source": _source("감축 전 원본이 있는 완료된 메시. 시드·형상 해상도는 원본 값을 쓴다"),
                    **outputs}, required=["source"])


def _game_asset(presets: dict[str, Any]) -> Schema:
    defaults = game_asset.game_defaults(presets)
    return _object({
        "source": _source("완료된 품질본 메시. 그 디테일을 저면 메시의 노멀·색·거칠기·AO 맵으로 굽는다"),
        "targetFaces": _integer(game_asset.MIN_FACES, game_asset.MAX_FACES, defaults["targetFaces"], "게임용 삼각형 예산"),
        "textureSize": _choice(defaults["textureSizes"], defaults["textureSize"], "구운 맵의 한 변 픽셀"),
        "ao": _boolean(defaults["ao"], "앰비언트 오클루전 맵을 함께 굽는다"),
        "sizeMeters": _number(game_asset.MIN_SIZE, game_asset.MAX_SIZE, None,
                              "가장 긴 변(m). 비우면 품질본 크기, 바닥 중심 기준으로 배율만 바꾼다"),
        "upright": _boolean(False, "가장 긴 축을 세로로 세우고 넓은 끝을 위로 둔다(도구·무기). 사진에서 보이던 면은 -Y 쪽"),
        "name": {"type": "string", "pattern": f"^{game_asset.NAME.pattern}$", "default": defaults["name"],
                 "description": "파일 이름. SM_<name>.fbx·T_<name>_*.png"},
    }, required=["source"])


def _building_kit(presets: dict[str, Any]) -> Schema:
    defaults = building_kit.kit_defaults(presets)
    return _object({
        "name": {"type": "string", "pattern": f"^{building_kit.NAME.pattern}$", "default": defaults["name"],
                 "description": "파일 이름 앞부분. SM_<name>_<부품>.fbx·T_<name>_kit_*.png"},
        "pieces": {"type": "array", "items": {"enum": list(building_kit.PIECES)},
                   "default": list(building_kit.PIECES), "description": "만들 부품. 비우면 전부"},
        "seed": _integer(0, 2**31 - 1, defaults["seed"], "판자 배치·색 변화"),
        "atlasWidth": _choice(defaults["atlasWidths"], defaults["atlasWidth"], "트림 시트 가로 픽셀(세로는 두 배)"),
        "cellMeters": _number(1.0, 16.0, defaults["cellMeters"], "구조 칸 한 변(m). 벽 길이는 칸 + 벽 두께"),
        "storyMeters": _number(2.0, 8.0, defaults["storyMeters"], "층 높이(m)"),
        "wallThickness": _number(0.1, 0.6, defaults["wallThickness"], "벽 두께(m)"),
        "referenceImage": _text(description="검수판에 조립한 집과 나란히 둘 콘셉트 이미지의 절대 경로"),
    })


def _import_image(_presets: dict[str, Any]) -> Schema:
    return _object({"uploadId": _image_source()["uploadId"], "name": _text(description="120자까지 쓴다")},
                   required=["uploadId"])


def _edit_layer() -> Schema:
    region = {
        "type": ["object", "null"],
        "description": "color 레이어. null은 에셋 전체. 2D 획은 [u, v, 반지름, 모드], 3D 획은 [x, y, z, 반지름(m), 모드]",
        "properties": {"strokes": {"type": "array", "maxItems": edit_asset.MAX_STROKES,
                                   "items": {"type": "array", "items": {"type": "number"}}}},
    }
    return _object({
        "id": {"type": "string", "pattern": edit_asset.LAYER_ID.pattern},
        "type": _choice(["color", "stamp"]),
        "name": _text(description="60자까지 쓴다"),
        "visible": _boolean(True),
        "locked": _boolean(False),
        "mode": _choice(["match", "fill"], "match", "color 레이어"),
        "from": {**COLOR, "description": "color 레이어. 바꿀 색"},
        "to": {**COLOR, "description": "color 레이어. 새 색"},
        "tolerance": _number(0.01, 1, 0.2, "color 레이어"),
        "region": region,
        "uploadId": {"type": "string", "pattern": UPLOAD_ID.pattern, "description": "stamp 레이어의 로고·문구 그림"},
        "opacity": _number(0, 1, 1, "stamp 레이어"),
        "rotation": _number(-180, 180, 0, "stamp 레이어"),
        "size": _number(0.001, 1000, 0.25, "stamp 레이어. 2D는 출력 가로 비율, 3D는 미터"),
        "text": _text(description="stamp 레이어. 160자까지 쓴다"),
        "textColor": {**COLOR, "description": "stamp 레이어"},
        "font": _choice(["bold", "regular", "serif"], description="stamp 레이어"),
        "x": _number(-1, 2, 0.5, "2D stamp 레이어"),
        "y": _number(-1, 2, 0.5, "2D stamp 레이어"),
        "position": _vector(3, -10000, 10000, "3D stamp 레이어. glTF 모델 좌표"),
        "normal": _vector(3, -10000, 10000, "3D stamp 레이어. 0 벡터는 받지 않는다"),
        "depth": _number(0.0001, 100, 0.03, "3D stamp 레이어"),
        "clip": _choice(["connected", "projection"], "connected", "3D stamp 레이어"),
    }, required=["id", "type"], allOf=[
        {"if": {"properties": {"type": {"const": "color"}}}, "then": {"required": ["to"]}},
        {"if": {"properties": {"type": {"const": "stamp"}}}, "then": {"required": ["uploadId"]}},
    ])


def _edit_asset(_presets: dict[str, Any]) -> Schema:
    crop = {"description": "회전·반전한 이미지 기준 정규화 사각형, 예전 비율 이름, 또는 null(원래 크기)", "anyOf": [
        _object({"x": _number(0, 0.99, 0), "y": _number(0, 0.99, 0), "w": _number(0.01, 1, 1), "h": _number(0.01, 1, 1)},
                description="x+w와 y+h는 1 이하"),
        _choice(["original", "square", "portrait", "landscape", "wide"]),
        {"type": "null"},
    ]}
    frame = _object({
        "turns": _integer(0, 3, 0),
        "flipX": _boolean(False),
        "crop": crop,
        "ratio": _choice(["original", "free", "square", "portrait", "landscape", "wide"], "original"),
        "padding": _number(0, 0.5, 0),
        "background": {**COLOR, "description": "여백 색. 비우면 투명"},
        "width": _integer(16, 4096, description="출력 가로 픽셀. 비우면 원래 크기"),
    }, description="2D만")
    plan = _object({
        "brightness": _number(0.1, 2, 1),
        "contrast": _number(0.1, 2, 1),
        "saturation": _number(0, 2, 1),
        "metallic": _number(0, 1, 1, "기존 PBR 계수에 곱할 비율"),
        "roughness": _number(0.05, 1, 1, "기존 PBR 계수에 곱할 비율"),
        "frame": frame,
        "layers": {"type": "array", "maxItems": edit_asset.MAX_LAYERS, "items": _edit_layer()},
    })
    return _object({
        "source": _source("편집할 이미지·메시 에셋"),
        "name": _text(description="120자까지 쓴다"),
        "replaceEdits": _boolean(False, "편집본의 기준 파일에 plan을 다시 적용한다"),
        "plan": plan,
    }, required=["source", "plan"])


def _previz(presets: dict[str, Any]) -> Schema:
    config = presets["previz"]
    defaults = config["defaults"]
    safe_id = {"type": "string", "pattern": previz.SAFE_ID.pattern}
    placement = _object({
        "source": _source("완성된 메시 에셋"),
        "path": _text(description="GLB·glTF 파일의 절대 경로"),
        "standin": _choice([standin["id"] for standin in config.get("standins", [])]),
        "size": _vector(3, 0.05, 500, "대역만. [가로, 깊이, 높이] 미터. 비우면 카탈로그 치수"),
        "id": safe_id,
        "position": _vector(3, -1000, 1000, "[x, y, z] 미터"),
        "yaw": _number(-360, 360, 0),
        "scale": _number(0.01, 100, 1.0),
    }, anyOf=_one_of_keys("standin", "source", "path"))
    framing = {name: _number(low, high) for key, (low, high) in previz.FRAMING_RANGES.items()
               for name in (key, f"{key}End")}
    shot = _object({
        "id": safe_id,
        "label": _text(description="40자까지 쓴다"),
        "purpose": _text(description="80자까지 쓴다"),
        "move": _text(description="24자까지 쓴다"),
        "focus": _text(description="hero, scene 또는 배치 id"),
        "lens": _number(8, 300, 35),
        "lensEnd": _number(8, 300),
        "seconds": _number(0.2, 60, 2.0),
        "ease": _choice(["inout", "linear"], "inout"),
        "framing": _object({**framing, "targetOffset": _vector(2, -5, 5)}, description="피사체 단위의 상대 구도"),
    }, required=["id"])
    return _object({
        "preset": _choice([preset["id"] for preset in config.get("shotPresets", [])], previz.DEFAULT_PRESET),
        "assets": {"type": "array", "minItems": 1, "maxItems": previz.MAX_ASSETS, "items": placement},
        "source": _source("배치가 하나일 때 assets 대신 쓰는 메시 에셋"),
        "path": _text(description="배치가 하나일 때 assets 대신 쓰는 GLB·glTF 절대 경로"),
        "shots": {"type": "array", "minItems": 1, "maxItems": previz.MAX_SHOTS, "items": shot,
                  "description": "고친 컷 목록. 비우면 프리셋 그대로"},
        "renderer": _choice(config["renderers"], defaults["renderer"]),
        "width": _integer(256, 1920, defaults["width"]),
        "height": _integer(144, 1080, defaults["height"]),
        "fps": _integer(6, 30, defaults["fps"]),
        "samples": _integer(1, 256, defaults["samples"]),
        "aux": _choice(config["auxModes"], defaults["aux"]),
        "animatic": _boolean(defaults["animatic"]),
        "ground": _boolean(defaults["ground"]),
        "clay": _boolean(defaults["clay"]),
        "description": _text(2000, "장면 설명 메모. 엔진은 다시 해석하지 않는다"),
    }, anyOf=_one_of_keys("assets", "source", "path"))


def _text_to_video(presets: dict[str, Any]) -> Schema:
    inputs = _image_inputs(presets, kind="video", max_count=1, default_count=1,
                           default_preset=presets.get("video", {}).get("defaultPreset") or video.DEFAULT_PRESET)
    # 컨셉 이미지는 영상 크기 그대로 한 장만 만든다. 장수·배경 제거는 받지 않는다.
    return _object({
        **{key: inputs[key] for key in ("subject", "preset", "style", "imageModel", "seed")},
        **_video_prompt(presets),
        "concept": _boolean(True, "false면 첫 프레임 없이 텍스트 모드로 만든다"),
        **_video_inputs(presets),
    }, required=["subject"])


def _image_to_video(presets: dict[str, Any]) -> Schema:
    return _object({
        **_image_source(),
        "name": _text(description="imagePath로 넣을 때의 제목"),
        "subject": _text(video.MAX_TEXT, "비우면 원본의 설명이나 파일 이름"),
        "style": _text(video.MAX_TEXT),
        "seed": _integer(0, MAX_SEED - 1, description="비우면 엔진이 무작위로 정해 기록한다"),
        **_video_prompt(presets),
        **_video_inputs(presets),
    }, anyOf=_one_of_keys("uploadId", "source", "imagePath"))


def _selftest(_presets: dict[str, Any]) -> Schema:
    return _object({
        "steps": _integer(1, selftest.MAX_STEPS, 2),
        "stepSeconds": _number(0.0, selftest.MAX_STEP_SECONDS, 0.5),
        "failAt": _integer(1, selftest.MAX_STEPS, description="1부터 세는 단계 번호. steps 이하. 그 단계에서 실패한다"),
    })


SCHEMAS: dict[str, Callable[[dict[str, Any]], Schema]] = {
    "image": _image,
    "text-to-3d": _text_to_3d,
    "image-to-3d": _image_to_3d,
    "repair-mesh": _repair_mesh,
    "refine-mesh": _refine_mesh,
    "game-asset": _game_asset,
    "building-kit": _building_kit,
    "import-image": _import_image,
    "edit-asset": _edit_asset,
    "previz": _previz,
    "text-to-video": _text_to_video,
    "image-to-video": _image_to_video,
    "selftest": _selftest,
}


def params_schema(recipe_id: str, presets: dict[str, Any]) -> Schema:
    """The request schema of a registered recipe; any object for one without a schema."""
    build = SCHEMAS.get(recipe_id)
    return build(presets) if build else {"type": "object"}
