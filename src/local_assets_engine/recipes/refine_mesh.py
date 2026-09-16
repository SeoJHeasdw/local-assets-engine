"""Rebuild quality assets from an immutable full-resolution model checkpoint."""

import shutil
from pathlib import Path

from ..jobs import JobNotFound
from ..presets import PresetError
from .base import Recipe
from .mesh import prepare_mesh_params, finish_quality_mesh


def prepare(params, presets, store):
    source = params.get("source")
    if not isinstance(source, dict):
        raise PresetError("품질을 다시 구성할 메시의 source가 필요합니다.")
    job_id, asset_id = str(source.get("jobId", "")), str(source.get("assetId", ""))
    try:
        job = store.load(job_id)
        if job["state"] != "done":
            raise PresetError("완료된 생성 원본만 다시 구성할 수 있습니다.")
        asset = next(a for a in job["assets"] if a["id"] == asset_id and a["kind"] == "mesh")
        meta = asset["meta"]
        state_file = meta.get("sourceStateFile")
        if not state_file and job["params"].get("audit"):
            state_file = "mesh/audit/decoded.npz"
        if not state_file:
            raise PresetError("이 작업에는 감축 전 원본이 없습니다. 입력 이미지로 새 3D 생성을 실행하세요.")
        state = store.resolve_file(job_id, state_file)
        image = store.resolve_file(job_id, "mesh/input.png")
    except (JobNotFound, KeyError, StopIteration) as error:
        raise PresetError("감축 전 원본과 입력 이미지를 찾을 수 없습니다.") from error
    selected = {**params, "pipelineType": meta["pipelineType"], "meshSeed": meta["seed"],
                "sizeMeters": params.get("sizeMeters", meta["sizeMeters"])}
    normalized = {**prepare_mesh_params(selected, presets),
                  "source": {"jobId": job_id, "assetId": asset_id},
                  "statePath": str(state), "imagePath": str(image),
                  "subject": job["params"].get("subject", job["title"])}
    return normalized, f"3D 품질 재구성 · {normalized['subject']}"


def run(ctx):
    mesh = ctx.dir / "mesh"
    mesh.mkdir()
    shutil.copyfile(ctx.params["statePath"], mesh / "source.npz")
    provenance = Path(ctx.params["statePath"]).with_suffix(".json")
    if provenance.is_file():
        shutil.copyfile(provenance, mesh / "source.json")
    shutil.copyfile(ctx.params["imagePath"], mesh / "input.png")
    finish_quality_mesh(ctx, ctx.params)


REFINE_MESH = Recipe("refine-mesh", "3D 품질 재구성", prepare, run)
