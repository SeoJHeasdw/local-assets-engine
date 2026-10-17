"""Game tree from CC0 scans: a procedural trunk with real bark, leaf cards cut from real scanned leaves, three
LODs in one FBX, a cross billboard and a stump. No generative model is used."""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING, Any

from ..materials import kit_materials, material_dir, missing, missing_files, scan_library
from ..presets import PresetError
from .base import Recipe, float_param, int_param

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,23}")
SOURCE_KEYS = ("source", "id", "page", "author", "method", "license", "licenseUrl", "sizeMeters", "dimensionsMeters",
               "resolution")
# 잎 아틀라스에서 형광 연두 픽셀이 이 비율을 넘으면 알린다(지형 질감과 같은 기준).
NEON_WARN = 0.02


def tree_defaults(presets: dict[str, Any]) -> dict[str, Any]:
    return presets["trees"]


def find_tree_preset(presets: dict[str, Any], preset_id: str) -> dict[str, Any]:
    for preset in tree_defaults(presets)["presets"]:
        if preset["id"] == preset_id:
            return preset
    raise PresetError(f"알 수 없는 나무 프리셋입니다: {preset_id}")


def _source(scan: dict[str, Any]) -> dict[str, Any]:
    return {key: scan[key] for key in SOURCE_KEYS if key in scan}


def _scaled(build: dict[str, Any], factor: float) -> dict[str, Any]:
    """The species' lengths scaled to another height: crown, fork and whorl spacing (girth is ``dbhMeters``)."""
    out = json.loads(json.dumps(build))
    out["heightMeters"] = round(build["heightMeters"] * factor, 4)
    out["crown"] = {**build["crown"], "base": build["crown"]["base"] * factor, "radius": build["crown"]["radius"] * factor}
    if build.get("trunk", {}).get("fork"):
        out["trunk"]["fork"] = build["trunk"]["fork"] * factor
    if "whorls" in build:
        out["whorls"]["spacing"] = build["whorls"]["spacing"] * factor
    return out


def prepare(params: dict[str, Any], presets: dict[str, Any], store: "JobStore") -> tuple[dict[str, Any], str]:
    defaults = tree_defaults(presets)
    preset_id = params.get("preset")
    if not preset_id:
        raise PresetError("나무 프리셋(preset)이 필요합니다.")
    preset = find_tree_preset(presets, str(preset_id))
    name = params.get("name") or preset["name"]
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise PresetError("name은 영문 소문자·숫자·_로 24자 이하이고, 소문자나 숫자로 시작해야 합니다.")
    library = scan_library(presets)
    bark_scan = library[preset["bark"]["scan"]]
    leaf_scan = library[preset["leaves"]["scan"]]
    ring_material = kit_materials(presets)[defaults["rings"]["material"]]
    gone = missing(presets, [bark_scan["id"], leaf_scan["id"]]) + missing_files(ring_material)
    if gone:
        raise PresetError("나무 스캔이 없습니다. `python -m local_assets_engine.materials fetch`로 받으세요 "
                          f"(없음: {', '.join(gone[:3])}).")
    build = preset["build"]
    height = float_param(params, "heightMeters", build["heightMeters"], 2.0, 40.0)
    build = _scaled(build, height / build["heightMeters"])
    build["dbhMeters"] = float_param(params, "dbhMeters", build["dbhMeters"], 0.05, 2.5)
    build["triangles"] = int_param(params, "triangles", build["triangles"], 1000, 20000)
    seed = int_param(params, "seed", preset["seed"], 0, 2**31 - 1)
    normalized = {
        "preset": preset["id"],
        "name": name,
        "seed": seed,
        "heightMeters": height,
        "textures": {
            "name": name, "seed": seed,
            "bark": {**{k: v for k, v in preset["bark"].items() if k != "scan"}, "size": defaults["barkSize"],
                     "scan": {"dir": str(material_dir(bark_scan)), **_source(bark_scan)}},
            "leaves": {**{k: v for k, v in preset["leaves"].items() if k != "scan"}, "id": leaf_scan["id"],
                       "dir": str(material_dir(leaf_scan))},
            "rings": {**{k: v for k, v in defaults["rings"].items() if k != "material"}, "id": ring_material["id"],
                      "dir": str(material_dir(ring_material))},
        },
        "build": {**build, "name": name, "seed": seed},
        "sources": {"bark": _source(bark_scan), "leaves": _source(leaf_scan), "rings": _source(ring_material)},
        "budget": defaults["budget"],
    }
    return normalized, f"{defaults['label']} · {preset['label']}"


def run(ctx: "JobContext") -> None:
    p = ctx.params
    out = ctx.dir / "tree"
    textures = out / "textures"
    textures.mkdir(parents=True)
    texture_spec, build_spec = out / "textures.json", out / "build.json"
    texture_spec.write_text(json.dumps(p["textures"], ensure_ascii=False, indent=2), "utf-8")
    build_spec.write_text(json.dumps(p["build"], ensure_ascii=False, indent=2), "utf-8")
    with ctx.stage("tree-textures", "껍질 타일·잎 원본·나이테") as stage:
        stage.run([sys.executable, "-m", "local_assets_engine.tools.tree_textures", "--spec", texture_spec,
                   "--output", textures])
    with ctx.stage("tree-build", "잎 카드·줄기·LOD·빌보드·그루터기") as stage:
        stage.run([sys.executable, "-m", "local_assets_engine.tools.tree_build", "--spec", build_spec,
                   "--textures", textures, "--output", out])
    report = json.loads((out / "tree.json").read_text("utf-8"))
    bark = json.loads((textures / "bark" / "bark.json").read_text("utf-8"))
    name = p["name"]
    maps = {f"bark_{key}": ctx.rel(textures / "bark" / bark["files"][key]) for key in ("basecolor", "normal", "mask")}
    maps.update({f"leaves_{key}": ctx.rel(out / value) for key, value in report["atlas"].items()})
    maps.update({f"billboard_{key}": ctx.rel(out / value) for key, value in report["billboard"]["files"].items()})
    files = {key: ctx.rel(out / value) for key, value in report["files"].items()}
    asset = ctx.add_asset(
        kind="mesh", role="final", file=out / report["files"]["glb"], preview=out / "preview.png",
        meta={"variant": "tree", "label": "나무", "name": f"tree_{name}", "preset": p["preset"],
              "files": {**files, **maps}, "reviewFile": ctx.rel(out / "review.png"),
              "sizeMeters": report["heightMeters"],
              "stats": {key: report[key] for key in ("heightMeters", "dbhMeters", "crownWidthMeters", "triangles", "wood",
                                                    "cards", "lodRatios", "boundsUnity", "barkTileMeters", "neonFraction")},
              "sources": p["sources"],
              "unity": {"axes": report["unityAxes"], "normal": "OpenGL +Y",
                        "mask": "R metallic 0, G occlusion, B leaf translucency, A smoothness",
                        "wind": "vertex color R trunk bend, G branch bend, B leaf flutter, A phase",
                        "barkTileMeters": report["barkTileMeters"]},
              "processingVersion": 1},
    )
    evidence = [out / "review.png", out / "tree.json"]
    if report["exportProblems"]:
        ctx.add_finding(severity="fail", code="mesh.game_export_mismatch", where="나무 FBX", asset_id=asset["id"],
                        params={"problems": report["exportProblems"]}, evidence=evidence)
    budget = p["budget"]
    t = report["triangles"]
    over = {}
    if not budget["lod0"][0] <= t["LOD0"] <= budget["lod0"][1]:
        over["LOD0"] = t["LOD0"]
    if not budget["wood"][0] <= report["wood"][0] <= budget["wood"][1]:
        over["wood"] = report["wood"][0]
    for lod, target in ((1, budget["lodRatios"][0]), (2, budget["lodRatios"][1])):
        if abs(report["lodRatios"][lod] - target) > budget["ratioTolerance"]:
            over[f"LOD{lod}Ratio"] = report["lodRatios"][lod]
    if not budget["stump"][0] <= t["stump"] <= budget["stump"][1]:
        over["stump"] = t["stump"]
    if over:
        ctx.add_finding(severity="warn", code="mesh.tree_budget", where="나무 면 예산", asset_id=asset["id"],
                        params={"outside": over, "budget": budget}, evidence=evidence)
    if report["neonFraction"] > NEON_WARN:
        ctx.add_finding(severity="warn", code="texture.neon_green", where="잎 아틀라스 색", asset_id=asset["id"],
                        params={"fraction": report["neonFraction"]}, evidence=[out / report["atlas"]["basecolor"]])


TREE = Recipe("tree", "나무", prepare, run)
