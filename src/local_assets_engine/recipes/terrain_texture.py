"""Seamless Unity terrain layer from CC0 scans: the tile's real size, a quilted period, a packed mask map."""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING, Any

from ..materials import material_dir, missing, scan_library
from ..presets import PresetError
from .base import Recipe, choice_param, float_param, int_param

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")
TOOL = [sys.executable, "-m", "local_assets_engine.tools.terrain_tile"]
# 타일 가장자리를 넘는 밝기·높이 변화가 이웃 픽셀 사이 평균 변화의 이 배를 넘으면 이음매를 알린다.
SEAM_WARN = 1.5
# 녹색 계열에서 채도·밝기가 높은(형광 연두) 픽셀이 이 비율을 넘으면 알린다.
NEON_WARN = 0.02
GRADE_RANGES = {"hueShift": (-60.0, 60.0), "saturation": (0.0, 2.0), "value": (0.5, 1.5), "maxSaturation": (0.05, 1.0)}
SOURCE_KEYS = ("source", "page", "author", "method", "license", "licenseUrl")


def terrain_defaults(presets: dict[str, Any]) -> dict[str, Any]:
    return presets["terrain"]


def find_terrain_preset(presets: dict[str, Any], preset_id: str) -> dict[str, Any]:
    for preset in terrain_defaults(presets)["presets"]:
        if preset["id"] == preset_id:
            return preset
    raise PresetError(f"알 수 없는 지형 프리셋입니다: {preset_id}")


def _scan(presets: dict[str, Any], scan_id: Any, roughness: Any = None) -> dict[str, Any]:
    scan = scan_library(presets).get(str(scan_id))
    if not scan or scan["kind"] != "texture":
        raise PresetError(f"scanLibrary에 없는 질감입니다: {scan_id}")
    entry = {"id": scan["id"], "dir": str(material_dir(scan)), "sizeMeters": float(scan["sizeMeters"]),
             **{key: scan[key] for key in SOURCE_KEYS if key in scan}}
    if roughness is not None:
        if (not isinstance(roughness, list) or len(roughness) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in roughness)):
            raise PresetError("roughness는 0~1 사이 [낮음, 높음]이어야 합니다.")
        entry["roughness"] = [float(v) for v in roughness]
    return entry


def _grade(value: Any) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(GRADE_RANGES):
        raise PresetError(f"grade는 {', '.join(GRADE_RANGES)}만 받습니다.")
    return {key: float_param(value, key, 0.0, *GRADE_RANGES[key]) for key in value}


def prepare(params: dict[str, Any], presets: dict[str, Any], store: "JobStore") -> tuple[dict[str, Any], str]:
    defaults = terrain_defaults(presets)
    preset_id = params.get("preset")
    if not preset_id:
        raise PresetError("지형 프리셋(preset)이 필요합니다.")
    preset = find_terrain_preset(presets, str(preset_id))
    name = params.get("name") or preset["name"]
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise PresetError("name은 영문 소문자·숫자·_로 40자 이하이고, 소문자나 숫자로 시작해야 합니다.")
    layer = _scan(presets, params.get("layer") or preset["layer"],
                  params.get("roughness", preset.get("roughness")))
    patch_params = params["patch"] if "patch" in params else preset.get("patch")
    patch = None
    if patch_params:
        if not isinstance(patch_params, dict):
            raise PresetError("patch는 {scan, coverage, noiseMeters} 형식이어야 합니다.")
        patch = {**_scan(presets, patch_params.get("scan"), patch_params.get("roughness")),
                 "coverage": float_param(patch_params, "coverage", 0.3, 0.02, 0.9),
                 "noiseMeters": float_param(patch_params, "noiseMeters", 0.3, 0.05, 10.0)}
    gone = missing(presets, [layer["id"]] + ([patch["id"]] if patch else []))
    if gone:
        raise PresetError("지형 스캔이 없습니다. `python -m local_assets_engine.materials fetch`로 받으세요 "
                          f"(없음: {', '.join(gone[:3])}).")
    grade = {**_grade(preset.get("grade")), **_grade(params.get("grade"))}
    normalized = {
        "preset": preset["id"],
        "name": name,
        "tileMeters": float_param(params, "tileMeters", preset["tileMeters"], 0.25, 32.0),
        "size": choice_param(params, "size", defaults["size"], defaults["sizes"]),
        "seed": int_param(params, "seed", preset.get("seed", 1), 0, 2**31 - 1),
        "patchMeters": float_param(params, "patchMeters", defaults["patchMeters"], 0.1, 4.0),
        "flatten": float_param(params, "flatten", defaults["flatten"], 0.0, 1.0),
        "flattenMeters": float_param(params, "flattenMeters", defaults["flattenMeters"], 0.1, 10.0),
        "layer": layer,
        "patch": patch,
        "grade": grade,
    }
    return normalized, f"{defaults['label']} · {preset['label']}"


def run(ctx: "JobContext") -> None:
    p = ctx.params
    out = ctx.dir / "terrain"
    out.mkdir()
    spec = out / "spec.json"
    spec.write_text(json.dumps(p, ensure_ascii=False, indent=2), "utf-8")
    with ctx.stage("terrain-tile", "질감 합성·맵 묶기") as stage:
        stage.run([*TOOL, "--spec", spec, "--output", out])
    report = json.loads((out / "tile.json").read_text("utf-8"))
    files = {key: ctx.rel(out / name) for key, name in report["files"].items()}
    sources = [{key: value for key, value in source.items() if key != "dir"}
               for source in (p["layer"], p.get("patch")) if source]
    asset = ctx.add_asset(
        kind="image", role="final", file=out / report["files"]["basecolor"], preview=out / report["files"]["preview"],
        meta={"variant": "terrain-layer", "label": "지형 질감", "name": p["name"], "preset": p["preset"],
              "tileMeters": p["tileMeters"], "width": p["size"], "height": p["size"], "files": files,
              "sources": sources, "stats": {key: report[key] for key in report if key not in ("files", "name")},
              "unity": {"tileSize": [p["tileMeters"], p["tileMeters"]], "normal": "OpenGL +Y",
                        "mask": "R metallic 0, G occlusion, B height, A smoothness"},
              "processingVersion": 1},
    )
    evidence = [out / report["files"]["preview"], out / "tile.json"]
    if report["wrapSeamRatio"] > SEAM_WARN:
        ctx.add_finding(severity="warn", code="texture.wrap_seam", where="지형 질감 가장자리", asset_id=asset["id"],
                        params={"ratio": report["wrapSeamRatio"]}, evidence=evidence)
    if report["neonFraction"] > NEON_WARN:
        ctx.add_finding(severity="warn", code="texture.neon_green", where="지형 질감 색", asset_id=asset["id"],
                        params={"fraction": report["neonFraction"]}, evidence=evidence)


TERRAIN_TEXTURE = Recipe("terrain-texture", "지형 질감", prepare, run)
