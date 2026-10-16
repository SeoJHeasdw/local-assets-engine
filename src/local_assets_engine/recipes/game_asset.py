"""Game-ready asset from a finished mesh: triangle budget, fresh UVs, maps baked from the master."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..jobs import JobNotFound
from ..presets import PresetError
from .base import Recipe, bool_param, choice_param, int_param
from .mesh import report_mesh_checks

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")
MIN_FACES, MAX_FACES = 100, 200_000
# 보이는 면의 95% 형상 오차가 가장 긴 변의 이 비율을 넘으면 사람이 보도록 알린다. 판정은 하지 않는다.
SHAPE_ERROR_WARN = 0.02
TOOL = [sys.executable, "-m", "local_assets_engine.tools.game_bake"]


def game_defaults(presets: dict[str, Any]) -> dict[str, Any]:
    return presets["mesh"]["gameAsset"]


def prepare(params: dict[str, Any], presets: dict[str, Any], store: "JobStore") -> tuple[dict[str, Any], str]:
    source = params.get("source")
    if not isinstance(source, dict):
        raise PresetError("게임용으로 만들 메시의 source가 필요합니다.")
    job_id, asset_id = str(source.get("jobId", "")), str(source.get("assetId", ""))
    try:
        job = store.load(job_id)
        asset = next(a for a in job["assets"] if a["id"] == asset_id and a["kind"] == "mesh")
        master = store.resolve_file(job_id, asset["file"])
    except (JobNotFound, KeyError, StopIteration) as error:
        raise PresetError("게임용으로 만들 메시를 찾을 수 없습니다.") from error
    if job["state"] != "done":
        raise PresetError("완료된 메시만 게임용으로 만들 수 있습니다.")
    if asset.get("meta", {}).get("variant") == "game-asset":
        raise PresetError("게임용 결과가 아니라 품질본을 고르세요. 굽기는 품질본의 디테일을 옮긴다.")
    defaults = game_defaults(presets)
    name = params.get("name") or defaults["name"]
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise PresetError("name은 영문 소문자·숫자·_로 40자 이하이고, 소문자나 숫자로 시작해야 합니다.")
    meta = asset.get("meta", {})
    normalized = {
        "source": {"jobId": job_id, "assetId": asset_id},
        "masterPath": str(master),
        "masterInspection": str(store.resolve_file(job_id, meta["inspectionFile"]).with_name("master-0.png"))
        if meta.get("inspectionFile") and meta.get("variant", "master") == "master" else None,
        "targetFaces": int_param(params, "targetFaces", defaults["targetFaces"], MIN_FACES, MAX_FACES),
        "textureSize": choice_param(params, "textureSize", defaults["textureSize"], defaults["textureSizes"]),
        "ao": bool_param(params, "ao", defaults["ao"]),
        "name": name,
        "sizeMeters": meta.get("sizeMeters"),
        "subject": job["params"].get("subject", job.get("title", "")),
    }
    return normalized, f"게임용 에셋 · {normalized['subject']}"


def run(ctx: "JobContext") -> None:
    p = ctx.params
    game = ctx.dir / "game"
    game.mkdir()
    master = Path(p["masterPath"])
    low, reduce_stats, bake_stats = game / "low.glb", game / "reduce.json", game / "bake.json"
    with ctx.stage("game-reduce", "면 줄이기·UV 펴기") as stage:
        stage.run([*TOOL, "reduce", "--master", master, "--output", low, "--stats", reduce_stats,
                   "--target-faces", p["targetFaces"], "--texture-size", p["textureSize"]])
    with ctx.stage("game-bake", "노멀·색·거칠기·AO 굽기") as stage:
        command = [*TOOL, "bake", "--master", master, "--low", low, "--reduce-stats", reduce_stats,
                   "--output-dir", game, "--stats", bake_stats, "--name", p["name"],
                   "--texture-size", p["textureSize"], "--samples", game_defaults(ctx.presets)["samples"]]
        if not p["ao"]:
            command.append("--no-ao")
        stage.run(command)
    low.unlink(missing_ok=True)
    reduced = json.loads(reduce_stats.read_text("utf-8"))
    baked = json.loads(bake_stats.read_text("utf-8"))
    glb = game / baked["files"]["glb"]

    inspection = game / "inspection"
    meshes = ["--mesh", f"game={glb}"]
    master_view = Path(p["masterInspection"]) if p.get("masterInspection") else None
    if not (master_view and master_view.is_file()):
        meshes += ["--mesh", f"master={master}"]
    with ctx.stage("inspect", "여러 각도 렌더 검수") as stage:
        stage.run([sys.executable, "-m", "local_assets_engine.tools.mesh_inspect", "--output", inspection, *meshes])
    if master_view and master_view.is_file():
        shutil.copyfile(master_view, inspection / "master-0.png")
    compare = game / "compare.png"
    with ctx.stage("compare", "품질본과 나란히 비교") as stage:
        stage.run([*TOOL, "compare", "--output", compare,
                   "--panel", f"품질본 · 삼각형 {reduced['sourceTriangles']:,}={inspection / 'master-0.png'}",
                   "--panel", f"게임용 · 삼각형 {reduced['triangles']:,} · {p['textureSize']}px={inspection / 'game-0.png'}"])

    stats = {"triangles": reduced["triangles"], "vertices": reduced["vertices"],
             "sourceTriangles": reduced["sourceTriangles"], "surfaceError": reduced["surfaceError"],
             "topology": reduced["topology"], "bake": baked}
    files = {key: ctx.rel(game / name) for key, name in baked["files"].items()}
    asset = ctx.add_asset(
        kind="mesh", role="final", file=glb, preview=inspection / "game-0.png",
        meta={"variant": "game-asset", "label": "게임용", "name": p["name"], "targetFaces": p["targetFaces"],
              "textureSize": p["textureSize"], "sizeMeters": p["sizeMeters"], "stats": stats, "files": files,
              "inspectionFile": ctx.rel(inspection / "game-contact.png"), "compareFile": ctx.rel(compare),
              "source": p["source"], "processingVersion": 2},
    )
    evidence = [compare, reduce_stats]
    report_mesh_checks(ctx, asset, stats, where="게임용 메시", evidence=evidence,
                       faces=(p["targetFaces"], reduced["triangles"]))
    error = reduced["surfaceError"]
    if error["p95Relative"] > SHAPE_ERROR_WARN:
        ctx.add_finding(severity="warn", code="mesh.game_shape_error", where="게임용 메시", asset_id=asset["id"],
                        params={"p95Meters": error["p95Meters"], "p95Relative": error["p95Relative"]},
                        evidence=evidence)


GAME_ASSET = Recipe("game-asset", "게임용 에셋 굽기", prepare, run)
