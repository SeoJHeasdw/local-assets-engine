"""Modular building kit: grid-exact pieces in Blender, one trim-sheet material from CC0 scans."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..materials import material_dir, missing
from ..presets import PresetError
from .base import Recipe, choice_param, float_param, int_param

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,23}")
PIECES = ("floor", "wall", "wall_door", "wall_window", "roof", "roof_slope", "stairs", "railing", "fence", "fence_gate")
# 부품 하나의 삼각형이 이 수를 넘으면 배치물 예산을 사람이 보도록 알린다. 판정은 하지 않는다.
TRIANGLE_WARN = 12_000


def _readable_image(path: Path) -> bool:
    from PIL import Image

    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def kit_defaults(presets: dict[str, Any]) -> dict[str, Any]:
    return presets["buildingKit"]


def prepare(params: dict[str, Any], presets: dict[str, Any], store: "JobStore") -> tuple[dict[str, Any], str]:
    defaults = kit_defaults(presets)
    name = params.get("name") or defaults["name"]
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise PresetError("name은 영문 소문자·숫자·_로 24자 이하이고, 소문자나 숫자로 시작해야 합니다.")
    pieces = params.get("pieces") or list(PIECES)
    if not isinstance(pieces, list) or not pieces or any(piece not in PIECES for piece in pieces):
        raise PresetError(f"pieces는 {', '.join(PIECES)} 중에서 고릅니다.")
    reference = params.get("referenceImage")
    if reference is not None and (not isinstance(reference, str) or not _readable_image(Path(reference))):
        raise PresetError("referenceImage는 읽을 수 있는 이미지 경로여야 합니다.")
    gone = missing(presets)
    if gone:
        raise PresetError("건축 키트 재질이 없습니다. `python -m local_assets_engine.materials fetch`로 받으세요 "
                          f"(없음: {', '.join(gone[:3])}).")
    normalized = {
        "name": name,
        "pieces": [piece for piece in PIECES if piece in pieces],
        "seed": int_param(params, "seed", defaults["seed"], 0, 2**31 - 1),
        "atlasWidth": choice_param(params, "atlasWidth", defaults["atlasWidth"], defaults["atlasWidths"]),
        "cellMeters": float_param(params, "cellMeters", defaults["cellMeters"], 1.0, 16.0),
        "storyMeters": float_param(params, "storyMeters", defaults["storyMeters"], 2.0, 8.0),
        "wallThickness": float_param(params, "wallThickness", defaults["wallThickness"], 0.1, 0.6),
        "referenceImage": reference,
        "materials": {role: str(material_dir(source)) for role, source in defaults["materials"].items()},
    }
    return normalized, f"{defaults['label']} · {name}"


def run(ctx: "JobContext") -> None:
    p = ctx.params
    kit = ctx.dir / "kit"
    atlas = kit / "atlas"
    with ctx.stage("kit-atlas", "트림 시트 만들기") as stage:
        stage.run([sys.executable, "-m", "local_assets_engine.tools.kit_atlas", "--planks", p["materials"]["planks"],
                   "--timber", p["materials"]["timber"], "--output", atlas, "--name", f"{p['name']}_kit",
                   "--width", p["atlasWidth"], "--seed", p["seed"]])
    with ctx.stage("kit-build", "부품 만들기·검수 렌더") as stage:
        stage.run([sys.executable, "-m", "local_assets_engine.tools.kit_build", "--atlas", atlas, "--output", kit,
                   "--name", p["name"], "--pieces", ",".join(p["pieces"]), "--seed", p["seed"],
                   "--cell", p["cellMeters"], "--story", p["storyMeters"], "--wall-thickness", p["wallThickness"],
                   *(["--reference", p["referenceImage"]] if p.get("referenceImage") else [])])
    report = json.loads((kit / "kit.json").read_text("utf-8"))
    layout = json.loads((atlas / "layout.json").read_text("utf-8"))
    maps = {key: ctx.rel(atlas / name) for key, name in layout["files"].items()}
    for piece in p["pieces"]:
        stats = report["pieces"][piece]
        asset = ctx.add_asset(
            kind="mesh", role="final", file=kit / stats["files"]["glb"], preview=kit / report["previews"][piece],
            meta={"variant": "building-kit", "label": "건축 부품", "name": f"{p['name']}_{piece}", "piece": piece,
                  "kit": p["name"], "stats": {"triangles": stats["triangles"], "boundsUnity": stats["boundsUnity"]},
                  "files": {**{key: ctx.rel(kit / name) for key, name in stats["files"].items()}, **maps},
                  "materials": stats["materials"], "grid": report["grid"], "unityAxes": report["unityAxes"],
                  "reviewFile": ctx.rel(kit / report["review"]),
                  "houseFile": ctx.rel(kit / report["house"]) if report["house"] else None,
                  "processingVersion": 1},
        )
        if stats["exportProblems"]:
            ctx.add_finding(severity="fail", code="mesh.game_export_mismatch", where="건축 부품 파일", asset_id=asset["id"],
                            params={"problems": stats["exportProblems"]}, evidence=[kit / "kit.json"])
        if stats["triangles"] > TRIANGLE_WARN:
            ctx.add_finding(severity="warn", code="mesh.kit_triangles", where="건축 부품", asset_id=asset["id"],
                            params={"triangles": stats["triangles"], "limit": TRIANGLE_WARN},
                            evidence=[kit / report["pieceSheet"]])


BUILDING_KIT = Recipe("building-kit", "건축 키트", prepare, run)
