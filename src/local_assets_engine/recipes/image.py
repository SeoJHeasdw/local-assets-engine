"""2D recipe: prompt → candidate images → background removal → canvas or pixel art."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from .. import imaging
from ..paths import engine_bin
from ..presets import build_prompt, find_preset
from .base import Recipe, bool_param, dimension_param, int_param, seed_param

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

DEFAULT_PRESET = "item-icon"
MAX_CANDIDATES = 8
PIXEL_PREVIEW_PX = 512


def prepare_image_params(
    params: dict[str, Any], presets: dict[str, Any], *,
    default_preset: str = DEFAULT_PRESET, default_count: int = 4,
) -> dict[str, Any]:
    preset = find_preset(presets, str(params.get("preset") or default_preset))
    subject = " ".join(str(params.get("subject") or "").split())
    style = " ".join(str(params.get("style") or "").split())
    prompt = build_prompt(preset, subject, style)
    count = int_param(params, "count", default_count, 1, MAX_CANDIDATES)
    base_seed = seed_param(params, count)
    remove_background = bool_param(params, "removeBackground", bool(preset.get("removeBackground")))
    return {
        "preset": preset["id"],
        "subject": subject,
        "style": style,
        "prompt": prompt,
        "count": count,
        "seeds": [base_seed + offset for offset in range(count)],
        "width": dimension_param(params, "width", preset.get("width", 1024)),
        "height": dimension_param(params, "height", preset.get("height", 1024)),
        "removeBackground": remove_background,
        "canvas": preset.get("canvas") if remove_background else None,
        "pixelate": preset.get("pixelate") if remove_background else None,
        "imageModel": presets["imageModel"]["id"],
    }


def run_background_removal(ctx: "JobContext", manifest: Path) -> None:
    config = ctx.presets["backgroundRemoval"]
    args: list[str | Path] = [
        sys.executable, "-m", "local_assets_engine.tools.remove_bg",
        "--manifest", manifest, "--repo", config["repo"],
    ]
    if config.get("revision"):
        args += ["--revision", config["revision"]]
    with ctx.stage("cutout", "배경 제거") as stage:
        stage.run(args, cwd=ctx.dir)


def generate_candidates(ctx: "JobContext", p: dict[str, Any], *, role: str = "candidate") -> list[dict[str, Any]]:
    model = ctx.presets["imageModel"]
    seeds = p["seeds"]
    raw_dir = ctx.dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    raws = [raw_dir / f"seed-{seed}.png" for seed in seeds]

    with ctx.stage("generate", "이미지 생성") as stage:
        stage.progress(0, f"{model['label']} · 후보 {len(seeds)}장", force=True)
        args: list[str | Path] = [
            engine_bin(model["command"]), "--prompt", p["prompt"],
            "--width", str(p["width"]), "--height", str(p["height"]),
            "--seed", *[str(seed) for seed in seeds],
            "--output", str(raw_dir / "seed-{seed}.png"), "--metadata",
        ]
        if model.get("steps"):
            args += ["--steps", str(model["steps"])]
        if model.get("quantize"):
            args += ["--quantize", str(model["quantize"])]
        stage.run(args, cwd=ctx.dir, units=len(seeds))
        missing = [path.name for path in raws if not path.exists()]
        if missing:
            raise RuntimeError(f"이미지 파일이 만들어지지 않았습니다: {', '.join(missing)}")

    sources = raws
    if p["removeBackground"]:
        cut_dir = ctx.dir / "cutout"
        cut_dir.mkdir(exist_ok=True)
        manifest = cut_dir / "manifest.json"
        items = [{"input": str(raw), "output": str(cut_dir / raw.name)} for raw in raws]
        manifest.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
        run_background_removal(ctx, manifest)
        sources = [cut_dir / raw.name for raw in raws]

    final_dir = ctx.dir / "final"
    final_dir.mkdir(exist_ok=True)
    results: list[dict[str, Any]] = []
    with ctx.stage("finish", "정리·자동 검사") as stage:
        for index, (seed, raw, source) in enumerate(zip(seeds, raws, sources), start=1):
            ctx.check_cancel()
            record: dict[str, Any] = {
                "seed": seed, "raw": raw, "file": source, "preview": source, "checks": {}, "error": None,
            }
            if p["removeBackground"]:
                _finish_cutout(record, Image.open(source), p, final_dir)
            results.append(record)
            stage.progress(index / len(seeds), f"{index}/{len(seeds)}")

    for record in results:
        with Image.open(record["file"]) as image:
            width, height = image.size
        record["asset"] = ctx.add_asset(
            kind="image", role=role, file=record["file"], preview=record["preview"],
            meta={
                "seed": record["seed"], "preset": p["preset"], "prompt": p["prompt"],
                "model": p["imageModel"], "width": width, "height": height,
                "checks": record["checks"], "error": record["error"], "raw": ctx.rel(record["raw"]),
            },
        )
    return results


def _finish_cutout(record: dict[str, Any], image: Image.Image, p: dict[str, Any], final_dir: Path) -> None:
    record["checks"] = imaging.cutout_checks(image)
    seed = record["seed"]
    try:
        fitted = image
        if canvas := p.get("canvas"):
            fitted = imaging.fit_to_canvas(image, canvas["width"], canvas["height"], canvas.get("padding", 0.06))
        target = final_dir / f"seed-{seed}.png"
        fitted.save(target)
        record["file"] = record["preview"] = target
        if pixel := p.get("pixelate"):
            small = imaging.pixelate(fitted, pixel["size"], pixel["colors"])
            pixel_path = final_dir / f"seed-{seed}-{pixel['size']}px.png"
            preview_path = final_dir / f"seed-{seed}-{pixel['size']}px@preview.png"
            small.save(pixel_path)
            imaging.upscale_nearest(small, max(1, PIXEL_PREVIEW_PX // max(small.size))).save(preview_path)
            record["file"], record["preview"] = pixel_path, preview_path
    except ValueError as error:
        record["error"] = str(error)


def _prepare(params: dict[str, Any], presets: dict[str, Any], _store: "JobStore") -> tuple[dict[str, Any], str]:
    normalized = prepare_image_params(params, presets)
    preset = find_preset(presets, normalized["preset"])
    return normalized, f"{preset['label']} · {normalized['subject']}"


def _run(ctx: "JobContext") -> None:
    generate_candidates(ctx, ctx.params)


IMAGE = Recipe(id="image", label="2D 이미지 후보", prepare=_prepare, run=_run)
