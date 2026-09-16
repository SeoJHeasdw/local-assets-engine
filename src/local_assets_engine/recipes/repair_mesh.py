"""Recover legacy KDTree exports in the engine lane, without model inference."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ..jobs import JobNotFound
from ..presets import PresetError
from .base import Recipe
from .mesh import finish_mesh, prepare_mesh_params

WORKER = Path(__file__).resolve().parents[1] / "workers" / "gltf_export.py"


def prepare(params, presets, store):
    source = params.get("source")
    if not isinstance(source, dict):
        raise PresetError("복구할 메시의 source: {jobId, assetId}가 필요합니다.")
    job_id, asset_id = str(source.get("jobId", "")), str(source.get("assetId", ""))
    try:
        job = store.load(job_id)
        asset = next(a for a in job["assets"] if a["id"] == asset_id and a["kind"] == "mesh")
        if job["state"] != "done" or job["recipe"] not in ("text-to-3d", "image-to-3d"):
            raise PresetError("완료된 이전 TRELLIS 생성 작업만 복구할 수 있습니다.")
        meta = asset["meta"]
        raw = store.resolve_file(job_id, meta["rawFile"])
        preview = store.resolve_file(job_id, asset["preview"])
        log = store.resolve_file(job_id, "job.log").read_text("utf-8")
        settings = {key: meta[key] for key in ("pipelineType", "textureSize", "targetFaces", "sizeMeters")}
        settings["meshSeed"] = meta["seed"]
    except (JobNotFound, StopIteration, KeyError) as error:
        raise PresetError("복구할 메시의 원본 GLB·입력 이미지·생성 기록을 찾지 못했습니다.") from error
    if meta.get("processingVersion") or "[local-assets] KDTree GLB export v" in log:
        raise PresetError("이미 내보내기 보정이 적용된 작업입니다.")
    if "Baking PBR textures via KDTree" not in log:
        raise PresetError("KDTree 대체 경로로 생성된 원본만 복구할 수 있습니다.")
    # Keep the source settings, including both resolution and face budget.
    normalized = {
        **prepare_mesh_params(settings, presets),
        "source": {"jobId": job_id, "assetId": asset_id},
        "rawPath": str(raw), "previewPath": str(preview),
        "subject": job["params"].get("subject", job["title"]),
    }
    return normalized, f"3D 복구 · {normalized['subject']}"


def run(ctx):
    mesh_dir = ctx.dir / "mesh"
    mesh_dir.mkdir()
    shutil.copyfile(ctx.params["previewPath"], mesh_dir / "input.png")
    with ctx.stage("repair", "기존 GLB 내보내기 보정") as stage:
        stage.run([
            sys.executable, WORKER, "--input", ctx.params["rawPath"],
            "--output", mesh_dir / "raw.glb",
        ])
    finish_mesh(ctx, ctx.params)


REPAIR_MESH = Recipe(id="repair-mesh", label="기존 3D 복구", prepare=prepare, run=run)
