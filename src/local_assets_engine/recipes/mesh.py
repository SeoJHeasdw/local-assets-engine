"""3D recipes: full source → reconstructed surface → final-geometry PBR."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from .. import imaging
from ..jobs import JobNotFound
from ..measure import StageFailed, parse_progress, was_killed
from ..paths import find_tool, trellis_generate_script, trellis_python
from ..presets import PresetError, find_preset
from ..runner import UnitTracker
from .base import Recipe, bool_param, choice_param, float_param, int_param, seed_param
from .image import generate_candidates, prepare_image_params, run_background_removal

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

WORKER = Path(__file__).resolve().parents[1] / "workers" / "trellis_runner.py"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class TrellisProgress:
    """Maps generate.py's printed milestones and sampler bars to one fraction."""

    MARKERS = (
        ("Loading pipeline", 0.02, "파이프라인 불러오는 중"),
        ("Loaded in", 0.25, "파이프라인 준비됨"),
        ("Generating 3D model", 0.28, "형태·질감 생성 중"),
        ("Mesh:", 0.80, None),
        ("Baking PBR", 0.84, "PBR 텍스처 굽는 중"),
        ("Saved:", 0.97, "파일 저장 중"),
    )

    def __init__(self) -> None:
        self.generating = False
        self.samplers = UnitTracker(3)

    def __call__(self, line: str) -> tuple[float | None, str | None] | None:
        for marker, fraction, detail in self.MARKERS:
            if marker in line:
                self.generating = marker == "Generating 3D model"
                return fraction, detail or line.strip()[:120]
        progress = parse_progress(line)
        if any(word in line for word in ("Fetching", "Downloading")):
            percent = f" {round(progress[0] * 100)}%" if progress else ""
            return None, f"모델 파일 내려받는 중{percent}"
        if self.generating and progress is not None:
            return 0.28 + 0.5 * self.samplers.update(progress[0]), None
        return None


def explain_trellis_failure(failure: StageFailed) -> str:
    if was_killed(failure):
        return ("메모리가 부족해 macOS가 TRELLIS 프로세스를 종료했습니다. "
                "3D 품질을 512로 낮추거나 다른 앱을 닫고 다시 시도하세요.")
    text = "\n".join(failure.tail)
    lowered = text.lower()
    if "gatedrepoerror" in lowered or ("dinov3" in lowered and any(code in text for code in ("401", "403"))):
        return "DINOv3 모델 접근 권한이 없습니다. docs/SETUP.md의 Hugging Face 단계를 먼저 진행하세요."
    if failure.code == 2 or "ImpactingInteractivity" in text:
        return "macOS GPU 감시가 긴 Metal 작업을 끊었습니다. 외부 모니터를 줄이고 다시 시도하세요."
    return f"TRELLIS.2 생성이 실패했습니다. ({failure})"


def prepare_mesh_params(params: dict[str, Any], presets: dict[str, Any]) -> dict[str, Any]:
    mesh = presets["mesh"]
    defaults = mesh["defaults"]
    return {
        "pipelineType": choice_param(params, "pipelineType", defaults["pipelineType"], mesh["pipelineTypes"]),
        "textureSize": choice_param(params, "textureSize", defaults["textureSize"], mesh["textureSizes"]),
        "targetFaces": int_param(params, "targetFaces", defaults["targetFaces"], 0, 1_000_000),
        "sizeMeters": float_param(params, "sizeMeters", defaults["sizeMeters"], 0.0, 1000.0),
        "meshSeed": seed_param({"seed": params.get("meshSeed")}),
        "audit": bool_param(params, "audit", False),
        "gameFaces": int_param(params, "gameFaces", defaults.get("gameFaces", 100000), 0, 1_000_000),
        "gameTextureSize": choice_param(params, "gameTextureSize", defaults.get("gameTextureSize", 2048), mesh["textureSizes"]),
    }


def run_mesh(ctx: "JobContext", image_path: Path, p: dict[str, Any], *, concept_asset_id: str | None = None) -> dict[str, Any]:
    python, script = trellis_python(), trellis_generate_script()
    if not python.exists() or not script.exists():
        raise RuntimeError("TRELLIS 엔진이 설치되지 않았습니다. docs/SETUP.md의 scripts/setup_trellis.sh를 실행하세요.")

    mesh_dir = ctx.dir / "mesh"
    mesh_dir.mkdir(exist_ok=True)
    input_path = mesh_dir / "input.png"
    with Image.open(image_path) as image:
        transparent = imaging.has_transparency(image)
        image.convert("RGBA" if transparent else "RGB").save(input_path)
    if not transparent and p.get("removeBackground", True):
        manifest = mesh_dir / "cutout-manifest.json"
        manifest.write_text(json.dumps([{"input": str(input_path), "output": str(input_path)}]), "utf-8")
        run_background_removal(ctx, manifest)

    background = ctx.presets["backgroundRemoval"]
    raw_base = mesh_dir / "raw"
    args: list[Any] = [python, WORKER, "--generate-py", script, "--birefnet", background["repo"]]
    if background.get("revision"):
        args += ["--birefnet-revision", background["revision"]]
    args += ["--state-output", mesh_dir / "source.npz"]
    if ctx.presets["mesh"].get("revision"):
        args += ["--model-revision", ctx.presets["mesh"]["revision"]]
    args += [
        "--", input_path, "--seed", p["meshSeed"], "--output", raw_base,
        "--pipeline-type", p["pipelineType"], "--texture-size", p["textureSize"],
    ]
    with ctx.stage("mesh", "3D 생성 (TRELLIS.2)") as stage:
        stage.progress(0, "파이프라인 불러오는 중", force=True)
        try:
            stage.run(args, cwd=mesh_dir, interpret=TrellisProgress(), retries=1)
        except StageFailed as failure:
            raise RuntimeError(explain_trellis_failure(failure)) from failure
        if not (mesh_dir / "source.npz").exists():
            raise RuntimeError("TRELLIS.2가 생성 원본을 저장하지 못했습니다.")

    return finish_quality_mesh(ctx, p, concept_asset_id=concept_asset_id)


def finish_quality_mesh(ctx: "JobContext", p: dict[str, Any], *, concept_asset_id: str | None = None) -> dict[str, Any]:
    """Full source → final topology → native PBR → normalize only → inspection."""
    mesh_dir = ctx.dir / "mesh"
    source = mesh_dir / "source.npz"
    workers = WORKER.parent
    python = trellis_python()
    with ctx.stage("surface", "원본 표면 재구성") as stage:
        stage.run([python, workers / "quality_surface.py", "--source", source,
                   "--output", mesh_dir / "surface", "--target-faces", p["targetFaces"],
                   "--game-faces", 0])
    surface = json.loads((mesh_dir / "surface/surface.json").read_text("utf-8"))
    variants = [("master", "품질본", p["textureSize"], "asset")]
    if p["gameFaces"]:
        with ctx.stage("lod", "게임용 형상 보존 감축") as stage:
            command = [python, workers / "quality_lod.py", "--master", mesh_dir / "surface/master.npz",
                       "--output", mesh_dir / "surface/game.npz", "--target-faces", p["gameFaces"]]
            if gltfpack := find_tool("gltfpack"):
                command += ["--gltfpack", gltfpack]
            stage.run(command)
        variants.append(("game", "게임용", p["gameTextureSize"], "asset.game"))
    artifacts = []
    for name, label, size, stem in variants:
        raw_glb = mesh_dir / f"{stem}.raw.glb"
        with ctx.stage(f"texture-{name}", f"{label} UV·PBR 굽기") as stage:
            stage.run([python, workers / "quality_texture.py", "--source", source,
                       "--geometry", mesh_dir / f"surface/{name}.npz", "--output", raw_glb,
                       "--texture-size", size])
        final_glb = mesh_dir / f"{stem}.glb"
        stats_path = mesh_dir / f"{stem}.stats.json"
        with ctx.stage(f"post-{name}", f"{label} 크기·원점 정리") as stage:
            command = [sys.executable, "-m", "local_assets_engine.tools.blender_post",
                       "--input", raw_glb, "--output", final_glb, "--stats", stats_path,
                       "--target-faces", 0, "--size", p["sizeMeters"]]
            if name == "game":
                command += ["--normalization", mesh_dir / "asset.stats.json"]
            stage.run(command)
        stats = json.loads(stats_path.read_text("utf-8"))
        stats["sourceTriangles"] = surface["sourceTriangles"]
        stats["reconstructedTriangles"] = surface["reconstructedTriangles"]
        if name == "game":
            stats["lod"] = json.loads((mesh_dir / "surface/game.json").read_text("utf-8"))
            stats["topology"]["warnings"].extend(stats["lod"]["warnings"])
        stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")
        optimized = None
        if gltfpack := find_tool("gltfpack"):
            optimized = mesh_dir / f"{stem}.opt.glb"
            with ctx.stage(f"optimize-{name}", f"{label} GLB 최적화") as stage:
                # No simplification ratio: this is transport optimization only.
                stage.run([gltfpack, "-i", final_glb, "-o", optimized, "-noq"])
        else:
            ctx.skip_stage(f"optimize-{name}", f"{label} GLB 최적화", "gltfpack이 없음")
        artifacts.append((name, label, size, final_glb, raw_glb, optimized, stats))
    with ctx.stage("inspect", "여러 각도 렌더 검수") as stage:
        command = [sys.executable, "-m", "local_assets_engine.tools.mesh_inspect", "--output", mesh_dir / "inspection"]
        for name, _label, _size, final_glb, *_ in artifacts:
            command += ["--mesh", f"{name}={final_glb}"]
        stage.run(command)
    assets = []
    for name, label, size, final_glb, raw_glb, optimized, stats in artifacts:
        assets.append(ctx.add_asset(
            kind="mesh", role="final", file=final_glb, preview=mesh_dir / f"inspection/{name}-0.png",
            meta={"seed": p["meshSeed"], "pipelineType": p["pipelineType"], "textureSize": size,
                  "targetFaces": p["targetFaces"] if name == "master" else p["gameFaces"],
                  "sizeMeters": p["sizeMeters"], "stats": stats, "variant": name, "label": label,
                  "rawFile": ctx.rel(raw_glb), "sourceStateFile": ctx.rel(source),
                  "optimizedFile": ctx.rel(optimized) if optimized else None,
                  "optimizedBytes": optimized.stat().st_size if optimized else None,
                  "inspectionFile": f"mesh/inspection/{name}-contact.png",
                  "source": p.get("source"), "conceptAsset": concept_asset_id,
                  "processingVersion": 2},
        ))
    return assets[0]


def finish_mesh(ctx: "JobContext", p: dict[str, Any], *, concept_asset_id: str | None = None) -> dict[str, Any]:
    """Shared measured postprocessing for new meshes and legacy recovery jobs."""
    mesh_dir = ctx.dir / "mesh"
    raw_glb = mesh_dir / "raw.glb"
    input_path = mesh_dir / "input.png"
    final_glb = mesh_dir / "asset.glb"
    stats_path = mesh_dir / "asset.stats.json"
    with ctx.stage("post", "메시 정리 (Blender)") as stage:
        post_args = [
            sys.executable, "-m", "local_assets_engine.tools.blender_post",
            "--input", raw_glb, "--output", final_glb, "--stats", stats_path,
            "--target-faces", p["targetFaces"], "--size", p["sizeMeters"],
        ]
        if p.get("audit"):
            post_args += ["--audit-dir", mesh_dir / "audit"]
        stage.run(post_args, cwd=mesh_dir)
    stats = json.loads(stats_path.read_text("utf-8"))
    # generate.py는 GLB와 함께 OBJ 사본을 남긴다. 소품 하나에 100MB가 넘어 보관하지 않는다.
    (mesh_dir / "raw.obj").unlink(missing_ok=True)

    optimized: Path | None = None
    if gltfpack := find_tool("gltfpack"):
        optimized = mesh_dir / "asset.opt.glb"
        with ctx.stage("optimize", "게임용 최적화 (gltfpack)") as stage:
            stage.run([gltfpack, "-i", final_glb, "-o", optimized], cwd=mesh_dir)
    else:
        ctx.skip_stage("optimize", "게임용 최적화 (gltfpack)", "gltfpack을 찾지 못해 건너뜀")

    return ctx.add_asset(
        kind="mesh", role="final", file=final_glb, preview=input_path,
        meta={
            "seed": p["meshSeed"], "pipelineType": p["pipelineType"], "textureSize": p["textureSize"],
            "targetFaces": p["targetFaces"], "sizeMeters": p["sizeMeters"], "stats": stats,
            "rawFile": ctx.rel(raw_glb),
            "optimizedFile": ctx.rel(optimized) if optimized else None,
            "optimizedBytes": optimized.stat().st_size if optimized else None,
            "source": p.get("source"), "conceptAsset": concept_asset_id,
            "processingVersion": 1,
        },
    )


def _prepare_image_to_3d(params: dict[str, Any], presets: dict[str, Any], store: "JobStore") -> tuple[dict[str, Any], str]:
    source = params.get("source")
    if source:
        job_id, asset_id = str(source.get("jobId")), str(source.get("assetId"))
        try:
            job = store.load(job_id)
            asset = next(a for a in job["assets"] if a["id"] == asset_id and a["kind"] == "image")
            image_path = store.resolve_file(job_id, asset["file"])
        except (JobNotFound, StopIteration) as error:
            raise PresetError("3D로 바꿀 이미지 후보를 찾을 수 없습니다.") from error
        subject = job["params"].get("subject") or asset_id
        source_ref: dict[str, str] | None = {"jobId": job_id, "assetId": asset_id}
    else:
        raw = str(params.get("imagePath") or "")
        image_path = Path(raw).expanduser()
        if not raw or not image_path.is_absolute() or not image_path.is_file() \
                or image_path.suffix.lower() not in IMAGE_SUFFIXES:
            raise PresetError("PNG·JPG·WEBP 이미지 파일의 절대 경로가 필요합니다.")
        subject, source_ref = image_path.stem, None
    normalized = {
        "imagePath": str(image_path),
        "source": source_ref,
        "removeBackground": bool_param(params, "removeBackground", True),
        **prepare_mesh_params(params, presets),
    }
    return normalized, f"3D · {subject}"


def _run_image_to_3d(ctx: "JobContext") -> None:
    run_mesh(ctx, Path(ctx.params["imagePath"]), ctx.params)


def _prepare_text_to_3d(params: dict[str, Any], presets: dict[str, Any], _store: "JobStore") -> tuple[dict[str, Any], str]:
    image_params = prepare_image_params(params, presets, default_preset="prop-3d", default_count=1)
    if find_preset(presets, image_params["preset"])["kind"] != "3d":
        raise PresetError("텍스트→3D에는 3D 프리셋이 필요합니다.")
    return {**image_params, **prepare_mesh_params(params, presets)}, f"3D 소품 · {image_params['subject']}"


def _run_text_to_3d(ctx: "JobContext") -> None:
    candidates = generate_candidates(ctx, ctx.params, role="concept")
    usable = [c for c in candidates if not c["error"] and c["checks"].get("objectFound", True)]
    if not usable:
        raise RuntimeError("3D로 바꿀 만한 컨셉 이미지가 나오지 않았습니다. 설명을 바꿔 다시 시도하세요.")
    chosen = usable[0]
    run_mesh(ctx, Path(chosen["file"]), ctx.params, concept_asset_id=chosen["asset"]["id"])


IMAGE_TO_3D = Recipe(id="image-to-3d", label="이미지 → 3D", prepare=_prepare_image_to_3d, run=_run_image_to_3d)
TEXT_TO_3D = Recipe(id="text-to-3d", label="텍스트 → 3D", prepare=_prepare_text_to_3d, run=_run_text_to_3d)
