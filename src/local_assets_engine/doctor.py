"""Environment checks: what this Mac can produce right now, and what is missing."""

from __future__ import annotations

import importlib.util
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import (
    MODEL_VIEWER_JS, child_env, engine_bin, find_tool, trellis_generate_script, trellis_python, video_python,
)
from .presets import image_models, load_presets
from .workers.video_runner import missing_weight_files
from .workers.model_cache import json_file, valid_safetensors, weight_files_missing

DINOV3_REPO = "facebook/dinov3-vitl16-pretrain-lvd1689m"
# deps/mtlmesh는 cumesh, deps/mtlgemm은 flex_gemm이라는 이름으로 설치된다.
TRELLIS_METAL_MODULES = ("mtldiffrast", "mtlbvh", "cumesh", "flex_gemm", "o_voxel")
QUALITY_MODULES = ("point_cloud_utils", "skimage", "xatlas", "scipy", "trimesh")
VIDEO_MODULES = ("torch", "diffusers", "transformers", "accelerate", "ftfy", "imageio_ffmpeg")


def hub_cache() -> Path:
    if cache := os.environ.get("HF_HUB_CACHE"):
        return Path(cache)
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"


def is_cached(repo: str, revision: str | None = None) -> bool:
    folder = hub_cache() / f"models--{repo.replace('/', '--')}"
    snapshots = folder / "snapshots"
    if not snapshots.is_dir() or not any(snapshots.iterdir()):
        return False
    # 내려받는 중이면 blobs에 .incomplete 파일이 남는다.
    if any((folder / "blobs").glob("*.incomplete")):
        return False
    if revision is None and (folder / "refs/main").is_file():
        revision = (folder / "refs/main").read_text().strip()
    candidates = [snapshots / revision] if revision else sorted(snapshots.iterdir())
    for snapshot in candidates:
        weights = list(snapshot.rglob("*.safetensors"))
        configs = list(snapshot.rglob("*.json"))
        complete_pipeline = snapshot_entrypoint_complete(repo, snapshot)
        if (weights and configs and all(valid_safetensors(path) for path in weights)
                and all(json_file(path) is not None for path in configs)
                and complete_pipeline
                and all(not weight_files_missing(path.parent) for path in snapshot.rglob("*.safetensors.index.json"))):
            return True
    return False


def snapshot_entrypoint_complete(repo: str, snapshot: Path) -> bool:
    if repo == "microsoft/TRELLIS.2-4B":
        pipeline = json_file(snapshot / "pipeline.json")
        args = pipeline.get("args") if pipeline else None
        declared = args.get("models") if isinstance(args, dict) else None
        if not isinstance(declared, dict) or not declared or not all(isinstance(v, str) for v in declared.values()):
            return False
        names = [name for name in declared.values() if name.startswith("ckpts/")]
        return bool(names) and all(".." not in Path(name).parts
                                   and json_file(snapshot / f"{name}.json") is not None
                                   and valid_safetensors(snapshot / f"{name}.safetensors") for name in names)
    if repo == "microsoft/TRELLIS-image-large":
        stem = snapshot / "ckpts/ss_dec_conv3d_16l8_fp16"
        return json_file(stem.with_suffix(".json")) is not None and valid_safetensors(stem.with_suffix(".safetensors"))
    if (snapshot / "transformer").is_dir():
        # mflux fetches the components it reads; FLUX.2's cache need not contain
        # a top-level model_index.json, but all three actual components are required.
        return all(json_file(snapshot / component / "config.json") is not None
                   and not weight_files_missing(snapshot / component) for component in ("transformer", "text_encoder", "vae"))
    config = json_file(snapshot / "config.json")
    if config is None:
        return False
    auto_map = config.get("auto_map") or {}
    if not isinstance(auto_map, dict):
        return False
    for value in auto_map.values():
        if isinstance(value, str):
            module = value.split("--")[-1].split(".")[0]
            if not (snapshot / f"{module}.py").is_file():
                return False
    return True


def snapshot_missing(repo: str, revision: str) -> list[str]:
    """Files the pinned snapshot still lacks. Works for partial downloads that left
    no ``.incomplete`` file behind (xet transfers)."""
    folder = hub_cache() / f"models--{repo.replace('/', '--')}"
    if any((folder / "blobs").glob("*.incomplete")):
        return ["받는 중인 파일(.incomplete)"]
    return missing_weight_files(folder / "snapshots" / revision)


def _hf_logged_in() -> bool:
    try:
        from huggingface_hub import get_token
    except ImportError:
        return False
    return bool(get_token())


def _run_python(python: Path, code: str, timeout: int = 90) -> tuple[bool, str]:
    if not python.exists():
        return False, "없음"
    try:
        result = subprocess.run(
            [str(python), "-c", code], capture_output=True, text=True, timeout=timeout, env=child_env(),
        )
    except subprocess.TimeoutExpired:
        return False, "시간 초과"
    lines = (result.stdout or result.stderr or "").strip().splitlines()
    return result.returncode == 0, (lines[-1] if lines else "")[:200]


def _metal_compiler() -> bool:
    try:
        return subprocess.run(["xcrun", "-f", "metal"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def build_report(*, deep: bool = True) -> dict[str, Any]:
    presets = load_presets()
    image_model, background, mesh = presets["imageModel"], presets["backgroundRemoval"], presets["mesh"]
    checks: dict[str, dict[str, Any]] = {}

    def add(key: str, label: str, ok: bool, *, detail: str = "", hint: str = "", required: bool = True) -> None:
        checks[key] = {"label": label, "ok": bool(ok), "detail": detail, "hint": hint, "required": required}

    add("appleSilicon", "Apple Silicon Mac", sys.platform == "darwin" and platform.machine() == "arm64",
        detail=platform.machine())
    add("imageCli", f"{image_model['label']} 실행기 (mflux)", engine_bin(image_model["command"]).exists(),
        hint="uv sync")
    add("imageWeights", f"{image_model['label']} 가중치", is_cached(image_model["repo"]), required=False,
        hint="첫 2D 생성 때 자동으로 내려받습니다.")
    for model in image_models(presets)[1:]:
        add(f"imageCli:{model['id']}", f"{model['label']} 실행기 (선택)", engine_bin(model["command"]).exists(),
            required=False, hint="uv sync")
        add(f"imageWeights:{model['id']}", f"{model['label']} 가중치 (선택)", is_cached(model["repo"]),
            required=False, hint=f"선택하면 첫 생성 때 내려받습니다: {model['repo']}")
    add("backgroundWeights", "BiRefNet 배경 제거 가중치", is_cached(background["repo"], background["revision"]), required=False,
        hint="첫 배경 제거 때 자동으로 내려받습니다.")
    add("bpy", "Blender 파이썬 모듈 (bpy)", importlib.util.find_spec("bpy") is not None, hint="uv sync")
    add("gltfpack", "gltfpack 게임용 최적화", find_tool("gltfpack") is not None, required=False,
        hint="npm i -g gltfpack")
    add("trellisEngine", "TRELLIS.2 Mac 엔진", trellis_python().exists() and trellis_generate_script().exists(),
        hint="scripts/setup_trellis.sh")
    add("trellisWeights", "TRELLIS.2 가중치", is_cached(mesh["repo"], mesh["revision"]), required=False,
        hint=f"hf download {mesh['repo']}")
    add("hfLogin", "Hugging Face 로그인", _hf_logged_in(), hint="hf auth login (docs/SETUP.md 1단계)")
    add("dinov3", "DINOv3 가중치 (승인 필요)", is_cached(DINOV3_REPO, mesh["dependencies"][DINOV3_REPO]), required=False,
        hint="접근 승인 뒤 첫 3D 생성 때 내려받습니다 (docs/SETUP.md 1단계).")
    add("trellisDecoder", "TRELLIS sparse decoder 고정 가중치",
        is_cached("microsoft/TRELLIS-image-large", mesh["dependencies"]["microsoft/TRELLIS-image-large"]),
        hint="docs/SETUP.md의 고정 커밋 가중치 준비")
    add("metalCompiler", "Xcode Metal 컴파일러", _metal_compiler(), required=False,
        hint="docs/SETUP.md 2단계")
    add("modelViewer", "앱 3D 미리보기 (model-viewer)", MODEL_VIEWER_JS.exists(), required=False,
        hint="npm install")
    ok, detail = _run_python(trellis_python(),
                            "import importlib.util as u; "
                            f"print(','.join(n for n in {QUALITY_MODULES!r} if not u.find_spec(n)) or 'ready')")
    add("qualityProcessing", "고품질 표면 재구성·PBR 도구", ok and detail == "ready",
        detail=detail, hint="scripts/setup_trellis.sh")
    if video := presets.get("videoModel"):
        ok, detail = _run_python(video_python(),
                                "import importlib.util as u; "
                                f"print(','.join(n for n in {VIDEO_MODULES!r} if not u.find_spec(n)) or 'ready')")
        add("videoEngine", f"{video['label']} 실행 환경 (diffusers)", ok and detail == "ready",
            detail=detail, hint="scripts/setup_video.sh")
        missing = snapshot_missing(video["repo"], video["revision"])
        add("videoWeights", f"{video['label']} 가중치 (@{video['revision'][:8]})", not missing,
            detail=", ".join(missing[:2]) + (f" 외 {len(missing) - 2}개" if len(missing) > 2 else ""),
            hint="docs/SETUP.md의 영상 모델 내려받기 명령 (작업 안에서는 받지 않습니다)")
        # Readiness of libraries/files is distinct from the measured 36GB
        # feasibility of the approved quality settings. Keep those settings.
        high_resolution = video["width"] * video["height"] >= 1280 * 704
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        add("videoMemoryBudget", "영상 기본 품질의 메모리 여유", not (high_resolution and memory <= 36 * 2**30),
            required=False, detail="준비 상태와 실제 생성 안정성은 다릅니다.",
            hint="현재 1280×704 fp32 VAE는 실측 48.2GB입니다. 같은 입력 비교 후 출력 설정을 선택하세요.")

    if deep:
        ok, detail = _run_python(Path(sys.executable), "import torch; print(torch.backends.mps.is_available())")
        add("mps", "PyTorch MPS", ok and detail == "True", detail=detail)
        code = ("import importlib.util as u; "
                f"print(','.join(n for n in {TRELLIS_METAL_MODULES!r} if u.find_spec(n)) or '-')")
        ok, detail = _run_python(trellis_python(), code)
        found = set(detail.split(",")) if ok else set()
        add("trellisMetal", "TRELLIS Metal 텍스처 가속", {"mtldiffrast", "mtlbvh", "flex_gemm"} <= found,
            detail=detail, required=False,
            hint="현재 품질 경로는 CPU 재구성·PBR 굽기를 사용합니다. 설치가 필요하지 않습니다.")

    ready = lambda *keys: all(checks[key]["ok"] for key in keys if key in checks)  # noqa: E731
    capabilities = {
        "image2d": ready("appleSilicon", "imageCli", "mps"),
        "mesh3d": ready("appleSilicon", "trellisEngine", "hfLogin", "bpy", "mps", "qualityProcessing",
                        "trellisWeights", "trellisDecoder", "dinov3", "backgroundWeights"),
        "meshTexture": ready("qualityProcessing"),
        "gameReady": ready("gltfpack"),
        # 프리비즈는 Blender만 쓴다. 이미지·3D 모델이 없어도 샷을 잡을 수 있다.
        "previz": ready("bpy"),
        # 가중치는 작업 안에서 내려받지 않으므로 받아 둔 것까지 갖춰야 만들 수 있다.
        "video": ready("appleSilicon", "videoEngine", "videoWeights", "mps") if "videoEngine" in checks else False,
    }
    return {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "machine": f"{platform.system()} {platform.machine()}",
        "memoryBytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
        "capabilities": capabilities,
        "checks": checks,
    }


CAPABILITY_LABELS = {
    "image2d": "2D 에셋 생성",
    "mesh3d": "3D 에셋 생성",
    "meshTexture": "원본 PBR 텍스처 굽기",
    "gameReady": "게임용 GLB 최적화",
    "previz": "프리비즈 샷 렌더",
    "video": "영상 생성",
}


def print_report(report: dict[str, Any]) -> None:
    print("Local Assets Engine 환경 진단")
    print(f"기기: {report['machine']} · 메모리 {report['memoryBytes'] / 2**30:.0f}GB")
    print()
    for key, label in CAPABILITY_LABELS.items():
        print(f"{'✓' if report['capabilities'][key] else '–'} {label}")
    print()
    for check in report["checks"].values():
        mark = "✓" if check["ok"] else ("✗" if check["required"] else "!")
        line = f"{mark} {check['label']}"
        if check["detail"] and not check["ok"]:
            line += f" ({check['detail']})"
        if check["hint"] and not check["ok"]:
            line += f" → {check['hint']}"
        print(line)
