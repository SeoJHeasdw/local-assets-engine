"""A scanned CC0 model as a quality master: aligned, cut flat where it sits in the ground, sized, graded.

The result is a ``master`` mesh like an image-to-3D result, so ``game-asset`` bakes it to a triangle budget.
"""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING, Any

from ..materials import file_path, missing, scan_library
from ..presets import PresetError
from .base import Recipe, float_param
from .mesh import report_mesh_checks

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")
# 남은 높이:폭을 맞추느라 가로를 이보다 더 늘리거나 줄였으면 사람이 보도록 알린다.
STRETCH_WARN = 0.2
SOURCE_KEYS = ("source", "id", "page", "author", "method", "license", "licenseUrl", "dimensionsMeters", "resolution")


def prepare(params: dict[str, Any], presets: dict[str, Any], store: "JobStore") -> tuple[dict[str, Any], str]:
    scan_id = str(params.get("scan") or "")
    scan = scan_library(presets).get(scan_id)
    if not scan or scan["kind"] != "model":
        models = [key for key, value in scan_library(presets).items() if value["kind"] == "model"]
        raise PresetError(f"scan은 scanLibrary의 모델 중 하나여야 합니다: {', '.join(models)}")
    if gone := missing(presets, [scan_id]):
        raise PresetError("스캔 모델이 없습니다. `python -m local_assets_engine.materials fetch`로 받으세요 "
                          f"(없음: {', '.join(gone[:3])}).")
    name = params.get("name") or scan_id
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise PresetError("name은 영문 소문자·숫자·_로 40자 이하이고, 소문자나 숫자로 시작해야 합니다.")
    if params.get("heightMeters") is None:
        raise PresetError("heightMeters(땅 위 높이, m)가 필요합니다.")
    normalized = {
        "scan": {key: scan[key] for key in SOURCE_KEYS if key in scan},
        "gltfPath": str(file_path(scan, scan["gltf"])),
        "name": name,
        "subject": name,
        "heightMeters": float_param(params, "heightMeters", 1.0, 0.02, 50.0),
        "widthMeters": float_param(params, "widthMeters", 1.0, 0.02, 100.0) if params.get("widthMeters") is not None
        else None,
        "bury": float_param(params, "bury", 0.05, 0.0, 0.6),
        "yaw": float_param(params, "yaw", 0.0, -180.0, 180.0),
        "saturation": float_param(params, "saturation", 1.0, 0.0, 2.0),
        "value": float_param(params, "value", 1.0, 0.5, 1.5),
    }
    return normalized, f"스캔 원본 · {name}"


def run(ctx: "JobContext") -> None:
    p = ctx.params
    scan = ctx.dir / "scan"
    scan.mkdir()
    master, stats_path = scan / "master.glb", scan / "prepare.json"
    with ctx.stage("scan-prepare", "정렬·바닥 자르기·크기·색 보정") as stage:
        stage.run([sys.executable, "-m", "local_assets_engine.tools.scan_prepare", "--gltf", p["gltfPath"],
                   "--output", master, "--stats", stats_path, "--height", p["heightMeters"],
                   *(["--width", p["widthMeters"]] if p.get("widthMeters") else []), "--bury", p["bury"],
                   "--yaw", p["yaw"], "--saturation", p["saturation"], "--value", p["value"]])
    stats = json.loads(stats_path.read_text("utf-8"))
    inspection = scan / "inspection"
    with ctx.stage("inspect", "여러 각도 렌더 검수") as stage:
        stage.run([sys.executable, "-m", "local_assets_engine.tools.mesh_inspect", "--output", inspection,
                   "--mesh", f"master={master}"])
    asset = ctx.add_asset(
        kind="mesh", role="final", file=master, preview=inspection / "master-0.png",
        meta={"variant": "master", "label": "스캔 원본", "name": p["name"], "sizeMeters": stats["sizeMeters"],
              "stats": stats, "scan": p["scan"], "inspectionFile": ctx.rel(inspection / "master-contact.png"),
              "processingVersion": 1},
    )
    report_mesh_checks(ctx, asset, stats, where="스캔 원본", evidence=[inspection / "master-contact.png", stats_path])
    if abs(stats["stretchXY"] - 1) > STRETCH_WARN:
        ctx.add_finding(severity="warn", code="mesh.scan_stretched", where="스캔 원본 크기", asset_id=asset["id"],
                        params={"stretchXY": stats["stretchXY"]}, evidence=[inspection / "master-contact.png", stats_path])


SCAN_MESH = Recipe("scan-mesh", "스캔 원본", prepare, run)
