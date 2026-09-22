"""Generate one clip with Wan2.2 TI2V-5B through diffusers on Apple Silicon (MPS).

This file runs in the video environment (``engines/video/.venv``, made by
``scripts/setup_video.sh``), not the engine's, so it must not import
``local_assets_engine``. Top-level imports stay in the standard library: the
engine's doctor reuses ``missing_weight_files`` without torch or diffusers.

The engine writes ``request.json``. This process writes the MP4, the first,
middle and last frames, and ``result.json``. The result echoes the prompt,
negative prompt and settings exactly as they were handed to the pipeline, so a
person can read them later and edit them for the next run.

Order keeps the 36GB machine inside its memory: the UMT5-XXL text encoder
(about 11GB in bf16) turns the prompts into embeddings and is released before
the DiT loads, and the DiT is released before the VAE decodes the latents.

Weights are read from the local Hugging Face cache only. A 34GB download never
starts inside a measured job; docs/SETUP.md has the download command.

Usage: python video_runner.py --request video/request.json
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Callable

PROGRESS_PREFIX = "@@progress "
PROGRESS_TOTAL = 1000
# 한 막대로 합친 단계별 몫. 잡음 제거가 시간 대부분을 쓴다.
SPANS = {"check": (0, 5), "encode": (5, 40), "load": (40, 80), "denoise": (80, 930),
         "decode": (930, 985), "write": (985, 1000)}
REQUIRED = ("mode", "repo", "revision", "prompt", "width", "height", "frames", "fps",
            "steps", "guidance", "seed", "outputDir", "result")
COMPONENTS = ("scheduler", "text_encoder", "tokenizer", "transformer", "vae")


def report(phase: str, fraction: float, detail: str) -> None:
    low, high = SPANS[phase]
    done = low + (high - low) * max(0.0, min(1.0, fraction))
    print(PROGRESS_PREFIX + json.dumps({"done": round(done, 1), "total": PROGRESS_TOTAL, "detail": detail},
                                       ensure_ascii=False), flush=True)


def load_request(path: Path) -> dict[str, Any]:
    request = json.loads(Path(path).read_text("utf-8"))
    if missing := [key for key in REQUIRED if request.get(key) in (None, "")]:
        raise SystemExit(f"영상 요청에 {', '.join(missing)} 값이 없습니다.")
    if request["mode"] not in ("t2v", "i2v"):
        raise SystemExit("mode는 t2v 또는 i2v여야 합니다.")
    if request["mode"] == "i2v" and not Path(str(request.get("image") or "")).is_file():
        raise SystemExit("첫 프레임 이미지 파일이 없습니다.")
    if int(request["width"]) % 32 or int(request["height"]) % 32:
        raise SystemExit("가로·세로는 32의 배수여야 합니다 (VAE 16배 × 패치 2).")
    if (int(request["frames"]) - 1) % 4:
        raise SystemExit("프레임 수는 4의 배수 + 1이어야 합니다 (VAE 시간 압축 4배).")
    return request


def missing_weight_files(snapshot: Path) -> list[str]:
    """Files a diffusers snapshot still lacks. An interrupted download leaves the
    small files in place, so a non-empty folder alone does not mean it is usable."""
    snapshot = Path(snapshot)
    if not (snapshot / "model_index.json").is_file():
        return ["model_index.json"]
    missing: list[str] = []
    for component in COMPONENTS:
        folder = snapshot / component
        if component == "tokenizer":
            if not (folder / "tokenizer.json").is_file() and not (folder / "spiece.model").is_file():
                missing.append(f"{component}/tokenizer.json")
            continue
        config = "scheduler_config.json" if component == "scheduler" else "config.json"
        if not (folder / config).is_file():
            missing.append(f"{component}/{config}")
        if component == "scheduler":
            continue
        indexes = sorted(folder.glob("*.safetensors.index.json"))
        if indexes:
            shards = set(json.loads(indexes[0].read_text("utf-8"))["weight_map"].values())
            missing += [f"{component}/{shard}" for shard in sorted(shards) if not (folder / shard).is_file()]
        elif not any(folder.glob("*.safetensors")):
            missing.append(f"{component}/*.safetensors")
    return missing


def key_frames(count: int) -> dict[str, int]:
    return {"first": 0, "middle": (count - 1) // 2, "last": count - 1}


class DiffusersBackend:
    """The only part that touches torch and diffusers. Tests replace it."""

    def __init__(self, request: dict[str, Any]):
        import torch

        if not torch.backends.mps.is_available():
            raise SystemExit("PyTorch MPS를 쓸 수 없습니다. Apple Silicon Mac에서 실행하세요.")
        self.torch = torch
        self.device = torch.device("mps")
        self.dtype = getattr(torch, request.get("dtype") or "bfloat16")
        self.vae_dtype = getattr(torch, request.get("vaeDtype") or "float32")
        self.repo, self.revision = request["repo"], request["revision"]
        self.pipe = None
        self.peak = 0

    def snapshot(self) -> Path:
        from huggingface_hub import snapshot_download

        try:
            return Path(snapshot_download(self.repo, revision=self.revision, local_files_only=True))
        except Exception as error:  # LocalEntryNotFoundError와 오프라인 오류를 한 안내로 모은다
            raise SystemExit(f"영상 모델 가중치가 없습니다: {self.repo}@{self.revision[:8]}. "
                             "docs/SETUP.md의 내려받기 명령을 먼저 실행하세요.") from error

    def _load(self, cls, **components):
        return cls.from_pretrained(self.repo, revision=self.revision, local_files_only=True,
                                   torch_dtype=self.dtype, **components)

    def sample_memory(self) -> None:
        self.peak = max(self.peak, int(self.torch.mps.driver_allocated_memory()))

    def release(self) -> None:
        gc.collect()
        self.torch.mps.empty_cache()

    def encode(self, prompt: str, negative_prompt: str | None, guidance: float, max_length: int):
        from diffusers import WanPipeline

        text = self._load(WanPipeline, transformer=None, vae=None).to(self.device)
        with self.torch.inference_mode():
            embeds, negative = text.encode_prompt(
                prompt=prompt, negative_prompt=negative_prompt, do_classifier_free_guidance=guidance > 1.0,
                num_videos_per_prompt=1, max_sequence_length=max_length, device=self.device,
            )
        self.sample_memory()
        del text
        self.release()
        return embeds, negative

    def load(self, mode: str) -> dict[str, Any]:
        from diffusers import AutoencoderKLWan, WanImageToVideoPipeline, WanPipeline

        # 모델 카드가 VAE만 float32로 불러온다. 복원 품질을 위해 그대로 따른다.
        vae = AutoencoderKLWan.from_pretrained(self.repo, subfolder="vae", revision=self.revision,
                                               local_files_only=True, torch_dtype=self.vae_dtype)
        cls = WanImageToVideoPipeline if mode == "i2v" else WanPipeline
        self.pipe = self._load(cls, vae=vae, text_encoder=None, tokenizer=None).to(self.device)
        self.pipe.set_progress_bar_config(disable=True)
        if mode == "i2v" and not self.pipe.config.expand_timesteps:
            raise SystemExit("이 모델은 첫 프레임을 latent로 넣는 방식(expand_timesteps)을 지원하지 않습니다.")
        self.sample_memory()
        scheduler = self.pipe.scheduler
        return {"pipeline": cls.__name__, "scheduler": type(scheduler).__name__,
                "flowShift": scheduler.config.get("flow_shift"), "solver": scheduler.config.get("solver_type")}

    def denoise(self, request: dict[str, Any], image, embeds, negative, on_step: Callable[[int], None]):
        generator = self.torch.Generator(device="cpu").manual_seed(int(request["seed"]))

        def step_end(_pipe, index, _timestep, tensors):
            self.sample_memory()
            on_step(index + 1)
            return tensors

        arguments = dict(
            prompt_embeds=embeds, negative_prompt_embeds=negative,
            height=int(request["height"]), width=int(request["width"]), num_frames=int(request["frames"]),
            num_inference_steps=int(request["steps"]), guidance_scale=float(request["guidance"]),
            generator=generator, output_type="latent", callback_on_step_end=step_end,
        )
        if request["mode"] == "i2v":
            arguments["image"] = image
        with self.torch.inference_mode():
            latents = self.pipe(**arguments).frames
        # 복원에는 VAE만 필요하다. DiT(bf16 약 10GB)를 먼저 내려 최대 메모리를 낮춘다.
        self.pipe.transformer = None
        self.release()
        return latents

    def decode(self, latents):
        """Same normalization and decode as the pipeline's non-latent output path (diffusers 0.40.0)."""
        torch, vae = self.torch, self.pipe.vae
        with torch.inference_mode():
            latents = latents.to(vae.dtype)
            shape = (1, vae.config.z_dim, 1, 1, 1)
            mean = torch.tensor(vae.config.latents_mean).view(shape).to(latents.device, latents.dtype)
            inverse_std = 1.0 / torch.tensor(vae.config.latents_std).view(shape).to(latents.device, latents.dtype)
            video = vae.decode(latents / inverse_std + mean, return_dict=False)[0]
            self.sample_memory()
            frames = self.pipe.video_processor.postprocess_video(video, output_type="np")[0]
        import numpy as np

        return (np.clip(frames, 0.0, 1.0) * 255.0).round().astype(np.uint8)

    def versions(self) -> dict[str, str]:
        import diffusers
        import transformers

        return {"python": platform.python_version(), "torch": self.torch.__version__,
                "diffusers": diffusers.__version__, "transformers": transformers.__version__}


def write_video(frames, path: Path, fps: int, encode: dict[str, Any]) -> None:
    import imageio_ffmpeg
    import numpy as np

    height, width = frames.shape[1:3]
    writer = imageio_ffmpeg.write_frames(
        str(path), (width, height), fps=fps, codec=encode.get("codec", "libx264"), quality=None,
        pix_fmt_out=encode.get("pixelFormat", "yuv420p"), macro_block_size=16,
        output_params=["-crf", str(encode.get("crf", 16)), "-preset", str(encode.get("preset", "slow")),
                       "-movflags", "+faststart"],
    )
    writer.send(None)
    for frame in frames:
        writer.send(np.ascontiguousarray(frame))
    writer.close()


def save_frame(frame, path: Path) -> None:
    from PIL import Image

    Image.fromarray(frame).save(path)


def run(request: dict[str, Any], backend, *, writer: Callable = write_video,
        frame_saver: Callable = save_frame) -> dict[str, Any]:
    out = Path(request["outputDir"])
    out.mkdir(parents=True, exist_ok=True)
    seconds: dict[str, float] = {}
    started = time.perf_counter()

    def timed(name: str, fn, *args):
        begin = time.perf_counter()
        value = fn(*args)
        seconds[name] = round(time.perf_counter() - begin, 3)
        return value

    report("check", 0, "가중치 확인 중")
    if missing := missing_weight_files(backend.snapshot()):
        raise SystemExit("영상 모델 가중치가 다 받아지지 않았습니다: " + ", ".join(missing[:4])
                         + ". docs/SETUP.md의 내려받기 명령을 다시 실행하면 이어서 받습니다.")

    prompt = request["prompt"]
    negative_prompt = request.get("negativePrompt") or None
    guidance = float(request["guidance"])
    report("encode", 0, "설명 해석 중 (UMT5)")
    embeds, negative = timed("encode", backend.encode, prompt, negative_prompt, guidance,
                             int(request.get("maxSequenceLength") or 512))

    report("load", 0, "영상 모델 불러오는 중")
    pipeline = timed("load", backend.load, request["mode"])

    image = None
    if request["mode"] == "i2v":
        from PIL import Image

        with Image.open(request["image"]) as source:
            image = source.convert("RGB")
        if image.size != (int(request["width"]), int(request["height"])):
            raise SystemExit(f"첫 프레임 크기 {image.size}가 요청 크기와 다릅니다. 엔진이 먼저 맞춰야 합니다.")

    steps = int(request["steps"])
    step_times: list[float] = []
    last = [time.perf_counter()]

    def on_step(done: int) -> None:
        now = time.perf_counter()
        step_times.append(round(now - last[0], 3))
        last[0] = now
        report("denoise", done / steps, f"잡음 제거 {done}/{steps}")

    report("denoise", 0, f"잡음 제거 0/{steps}")
    last[0] = time.perf_counter()
    latents = timed("denoise", backend.denoise, request, image, embeds, negative, on_step)

    report("decode", 0, "프레임 복원 중 (VAE)")
    frames = timed("decode", backend.decode, latents)

    report("write", 0, "MP4 저장 중")
    begin = time.perf_counter()
    video = out / "video.mp4"
    writer(frames, video, int(request["fps"]), request.get("encode") or {})
    names = {}
    for name, index in key_frames(len(frames)).items():
        names[name] = f"{name}.png"
        frame_saver(frames[index], out / names[name])
    seconds["write"] = round(time.perf_counter() - begin, 3)
    seconds["total"] = round(time.perf_counter() - started, 3)

    result = {
        "mode": request["mode"],
        "model": {"repo": request["repo"], "revision": request["revision"],
                  "dtype": request.get("dtype"), "vaeDtype": request.get("vaeDtype"), **pipeline},
        # 파이프라인에 실제로 넘긴 값. 다음 실행에서 고칠 기준이다.
        "sent": {
            "prompt": prompt, "negativePrompt": negative_prompt, "image": request.get("image"),
            "width": int(request["width"]), "height": int(request["height"]), "frames": len(frames),
            "fps": int(request["fps"]), "steps": steps, "guidance": guidance, "seed": int(request["seed"]),
            "maxSequenceLength": int(request.get("maxSequenceLength") or 512),
        },
        "files": {"video": "video.mp4", **names},
        "seconds": seconds,
        "stepSeconds": step_times,
        "mpsPeakBytes": backend.peak or None,
        "versions": backend.versions(),
    }
    Path(request["result"]).write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    report("write", 1, "완료")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    # huggingface_hub이 import될 때 읽는다. 작업 안에서는 어떤 네트워크 요청도 하지 않는다.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    # MPS에 없는 연산은 CPU로 돈다. 경고가 job.log에 남으므로 느려진 원인을 추적할 수 있다.
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    request = load_request(args.request)
    run(request, DiffusersBackend(request))


if __name__ == "__main__":
    sys.exit(main())
