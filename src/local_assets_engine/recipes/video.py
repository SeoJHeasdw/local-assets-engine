"""Video recipes: a first frame and a prompt → one Wan2.2 TI2V clip.

``text-to-video`` draws the first frame with the image model, the same concept
step as ``text-to-3d``, then animates it. ``image-to-video`` starts from an
existing image. Both run the model in its own environment through
``workers/video_runner.py`` under the measured stage runner.

The first frame fixes the composition, so shot size and angle go into the
concept prompt as well as the video prompt; camera movement only makes sense
for the video. The video prompt is assembled here, stored in ``params``, and
echoed back by the worker as what the pipeline actually received. A request
may send ``videoPrompt`` to replace the assembled text verbatim.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from .. import imaging
from ..measure import PROGRESS_PREFIX, StageFailed, is_gpu_busy, parse_progress, was_killed
from ..paths import video_python
from ..presets import CAMERA_PARTS, PresetError, build_prompt, find_preset, find_video_model
from .base import Recipe, bool_param, float_param, int_param, seed_param
from .image import generate_candidates, prepare_image_params, resolve_image_source

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

WORKER = Path(__file__).resolve().parents[1] / "workers" / "video_runner.py"
MAX_TEXT = 2000
DEFAULT_PRESET = "video-cinematic"


def text_param(params: dict[str, Any], key: str, limit: int = MAX_TEXT) -> str:
    value = params.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PresetError(f"{key}는 글자여야 합니다.")
    value = " ".join(value.split())
    if len(value) > limit:
        raise PresetError(f"{key}는 {limit}자 이하여야 합니다.")
    return value


def camera_param(params: dict[str, Any], presets: dict[str, Any]) -> dict[str, str]:
    """``{shot, angle, move}`` ids from the preset vocabulary. Unset parts add no words."""
    raw = params.get("camera") or {}
    if not isinstance(raw, dict) or set(raw) - set(CAMERA_PARTS):
        raise PresetError(f"camera는 {{{', '.join(CAMERA_PARTS)}}} 중에서 고른 id여야 합니다.")
    options = presets.get("video", {}).get("camera", {})
    chosen: dict[str, str] = {}
    for part in CAMERA_PARTS:
        if raw.get(part) in (None, ""):
            continue
        if not any(option["id"] == raw[part] for option in options.get(part, [])):
            raise PresetError(f"알 수 없는 카메라 {part}입니다: {raw[part]}")
        chosen[part] = str(raw[part])
    return chosen


def camera_text(camera: dict[str, str], presets: dict[str, Any], parts: tuple[str, ...] = CAMERA_PARTS) -> str:
    options = presets.get("video", {}).get("camera", {})
    return ", ".join(next(option["text"] for option in options[part] if option["id"] == camera[part])
                     for part in parts if part in camera)


def build_video_prompt(subject: str, motion: str, camera: str, style: str) -> str:
    parts = [part.rstrip(" .") for part in (subject, motion, camera, style) if part]
    return ". ".join(parts) + "." if parts else ""


def prepare_video_params(params: dict[str, Any], presets: dict[str, Any], *, portrait: bool = False) -> dict[str, Any]:
    """Size, length and sampler settings. Defaults are the model config's values."""
    model = find_video_model(presets, params.get("videoModel"))
    default_width, default_height = (model["height"], model["width"]) if portrait else (model["width"], model["height"])
    width = int_param(params, "width", default_width, 256, 2048)
    height = int_param(params, "height", default_height, 256, 2048)
    # VAE 16배 × 패치 2라 32의 배수만 그대로 쓴다. 파이프라인이 몰래 줄이지 않게 여기서 내린다.
    width, height = width - width % 32, height - height % 32
    if width * height > model["maxPixels"]:
        raise PresetError(f"가로×세로는 {model['maxPixels']:,}픽셀({model['width']}×{model['height']}) 이하여야 합니다.")
    frames = int_param(params, "frames", model["frames"], 5, model["maxFrames"])
    frames -= (frames - 1) % 4
    return {
        "videoModel": model["id"],
        # 요청이 대기하는 동안 설정 파일이 바뀌어도 고른 조건으로 실행한다.
        "videoModelConfig": deepcopy(model),
        "width": width, "height": height, "frames": frames, "fps": model["fps"],
        "seconds": round(frames / model["fps"], 2),
        "steps": int_param(params, "steps", model["steps"], 1, 100),
        "guidance": float_param(params, "guidance", model["guidance"], 1.0, 20.0),
        "negativePrompt": text_param(params, "negativePrompt") if "negativePrompt" in params
        else model.get("negativePrompt", ""),
    }


def prepare_video_prompt(params: dict[str, Any], presets: dict[str, Any], *, subject: str, style: str) -> dict[str, Any]:
    camera = camera_param(params, presets)
    motion = text_param(params, "motion")
    sentence = camera_text(camera, presets)
    edited = text_param(params, "videoPrompt")
    prompt = edited or build_video_prompt(subject, motion, sentence, style)
    if not prompt:
        raise PresetError("무엇이 어떻게 움직일지 적어 주세요.")
    return {
        "camera": camera, "motion": motion, "videoPrompt": prompt, "videoPromptEdited": bool(edited),
        "videoPromptParts": {"subject": subject, "motion": motion, "camera": sentence, "style": style},
    }


def video_progress(line: str) -> tuple[float | None, str | None] | None:
    # 모델 적재 중 transformers·diffusers가 그리는 tqdm 막대는 전체 진행과 무관하다.
    return parse_progress(line) if line.startswith(PROGRESS_PREFIX) else None


def explain_video_failure(failure: StageFailed) -> str:
    if was_killed(failure):
        return ("메모리가 부족해 macOS가 영상 프로세스를 종료했습니다. "
                "해상도나 프레임 수를 줄이거나 다른 앱을 닫고 다시 시도하세요.")
    if "out of memory" in "\n".join(failure.tail).lower():
        return ("MPS 메모리가 부족해 영상 생성이 멈췄습니다. "
                "해상도나 프레임 수를 줄이거나 다른 앱을 닫고 다시 시도하세요.")
    for line in reversed(failure.tail):
        if "docs/SETUP.md" in line:
            return line.strip()
    if is_gpu_busy(failure):
        return "macOS GPU 감시가 긴 Metal 작업을 끊었습니다. 외부 모니터를 줄이고 다시 시도하세요."
    return f"영상 생성이 실패했습니다. ({failure})"


def run_video(ctx: "JobContext", p: dict[str, Any], *, image: Path | None,
              concept_asset_id: str | None = None) -> dict[str, Any]:
    python = video_python()
    if not python.exists():
        raise RuntimeError("영상 엔진이 설치되지 않았습니다. docs/SETUP.md의 scripts/setup_video.sh를 실행하세요.")
    model = p["videoModelConfig"]
    video_dir = ctx.dir / "video"
    video_dir.mkdir(exist_ok=True)
    input_path, crop = None, None
    if image is not None:
        input_path = video_dir / "input.png"
        with Image.open(image) as source:
            framed, crop = imaging.cover(source, p["width"], p["height"])
        framed.save(input_path)
    request = {
        "mode": "i2v" if input_path else "t2v",
        "repo": model["repo"], "revision": model["revision"],
        "dtype": model.get("dtype"), "vaeDtype": model.get("vaeDtype"),
        "prompt": p["videoPrompt"], "negativePrompt": p["negativePrompt"],
        "image": str(input_path) if input_path else None,
        "width": p["width"], "height": p["height"], "frames": p["frames"], "fps": p["fps"],
        "steps": p["steps"], "guidance": p["guidance"], "seed": p["videoSeed"],
        "maxSequenceLength": model.get("maxSequenceLength", 512), "encode": model.get("encode", {}),
        "outputDir": str(video_dir), "result": str(video_dir / "result.json"),
    }
    request_path = video_dir / "request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), "utf-8")
    with ctx.stage("video", f"영상 생성 ({model['label']})") as stage:
        stage.progress(0, f"{p['width']}×{p['height']} · {p['frames']}프레임 · {p['steps']}스텝", force=True)
        try:
            stage.run([python, WORKER, "--request", request_path], cwd=video_dir,
                      interpret=video_progress, retries=1)
        except StageFailed as failure:
            raise RuntimeError(explain_video_failure(failure)) from failure

    result = json.loads((video_dir / "result.json").read_text("utf-8"))
    sent, files = result["sent"], result["files"]
    return ctx.add_asset(
        kind="video", role="final", file=video_dir / files["video"], preview=video_dir / files["middle"],
        meta={
            "model": p["videoModel"], "repo": model["repo"], "revision": model["revision"],
            "mode": result["mode"], "pipeline": result["model"].get("pipeline"),
            "scheduler": result["model"].get("scheduler"), "flowShift": result["model"].get("flowShift"),
            # 파이프라인이 실제로 받은 문장. 다음 요청의 videoPrompt로 고쳐 보낼 수 있다.
            "prompt": sent["prompt"], "negativePrompt": sent["negativePrompt"],
            "promptEdited": p["videoPromptEdited"], "promptParts": p["videoPromptParts"], "camera": p["camera"],
            "width": sent["width"], "height": sent["height"], "frames": sent["frames"], "fps": sent["fps"],
            "seconds": round(sent["frames"] / sent["fps"], 2), "steps": sent["steps"],
            "guidance": sent["guidance"], "seed": sent["seed"],
            "inputImage": ctx.rel(input_path) if input_path else None, "crop": crop,
            "firstFrame": ctx.rel(video_dir / files["first"]), "lastFrame": ctx.rel(video_dir / files["last"]),
            "requestFile": ctx.rel(request_path), "resultFile": ctx.rel(video_dir / "result.json"),
            "timings": result["seconds"], "mpsPeakBytes": result.get("mpsPeakBytes"),
            "versions": result.get("versions"),
            "source": p.get("source"), "conceptAsset": concept_asset_id,
        },
    )


def _prepare_text_to_video(params: dict[str, Any], presets: dict[str, Any], _store: "JobStore") -> tuple[dict[str, Any], str]:
    preset_id = str(params.get("preset") or presets.get("video", {}).get("defaultPreset") or DEFAULT_PRESET)
    preset = find_preset(presets, preset_id, kind="video")
    video = prepare_video_params(params, presets)
    image = prepare_image_params(
        {**params, "preset": preset["id"], "count": 1, "width": video["width"], "height": video["height"],
         "removeBackground": False},
        presets, default_preset=preset["id"], default_count=1,
    )
    camera = camera_param(params, presets)
    # 첫 프레임이 구도를 정한다. 움직임은 한 장에 담을 수 없어 영상 설명에만 넣는다.
    composition = camera_text(camera, presets, ("shot", "angle"))
    image["prompt"] = build_prompt(preset, image["subject"], ", ".join(filter(None, (composition, image["style"]))))
    style = ", ".join(filter(None, (preset.get("videoStyle"), image["style"])))
    normalized = {
        **image, **video,
        **prepare_video_prompt(params, presets, subject=image["subject"], style=style),
        # false면 컨셉 이미지 없이 TI2V의 텍스트 모드로 만든다. 같은 시드로 비교할 때 쓴다.
        "concept": bool_param(params, "concept", True),
        "videoSeed": image["seeds"][0],
    }
    return normalized, f"영상 · {image['subject']}"


def _run_text_to_video(ctx: "JobContext") -> None:
    p = ctx.params
    if not p["concept"]:
        run_video(ctx, p, image=None)
        return
    candidates = generate_candidates(ctx, p, role="concept")
    usable = [candidate for candidate in candidates if not candidate["error"]]
    if not usable:
        raise RuntimeError("첫 프레임으로 쓸 컨셉 이미지가 나오지 않았습니다. 설명을 바꿔 다시 시도하세요.")
    chosen = usable[0]
    run_video(ctx, p, image=Path(chosen["file"]), concept_asset_id=chosen["asset"]["id"])


def _prepare_image_to_video(params: dict[str, Any], presets: dict[str, Any], store: "JobStore") -> tuple[dict[str, Any], str]:
    image_path, source_subject, source_ref = resolve_image_source(
        params, store, not_found="영상으로 만들 이미지를 찾을 수 없습니다.")
    with Image.open(image_path) as image:
        portrait = image.height > image.width
    subject = text_param(params, "subject") or source_subject
    style = text_param(params, "style")
    normalized = {
        "subject": subject, "style": style, "imagePath": str(image_path), "source": source_ref,
        **prepare_video_params(params, presets, portrait=portrait),
        **prepare_video_prompt(params, presets, subject=subject, style=style),
        "videoSeed": seed_param(params),
    }
    return normalized, f"영상 · {subject}"


def _run_image_to_video(ctx: "JobContext") -> None:
    run_video(ctx, ctx.params, image=Path(ctx.params["imagePath"]))


TEXT_TO_VIDEO = Recipe(id="text-to-video", label="텍스트 → 영상", prepare=_prepare_text_to_video, run=_run_text_to_video)
IMAGE_TO_VIDEO = Recipe(id="image-to-video", label="이미지 → 영상", prepare=_prepare_image_to_video, run=_run_image_to_video)
