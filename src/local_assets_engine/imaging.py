"""Cheap image post-processing that runs inside the engine process."""

from __future__ import annotations

import numpy as np
from PIL import Image

ALPHA_THRESHOLD = 8


def has_transparency(image: Image.Image) -> bool:
    if image.mode not in ("RGBA", "LA", "PA") and "transparency" not in image.info:
        return False
    alpha = np.asarray(image.convert("RGBA"))[:, :, 3]
    return bool((alpha < 255).any())


def alpha_bbox(image: Image.Image, threshold: int = ALPHA_THRESHOLD) -> tuple[int, int, int, int] | None:
    alpha = np.asarray(image.convert("RGBA"))[:, :, 3]
    ys, xs = np.nonzero(alpha > threshold)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def cutout_checks(image: Image.Image) -> dict[str, object]:
    """Automatic signals only. They never replace the user's visual approval."""
    rgba = image.convert("RGBA")
    box = alpha_bbox(rgba)
    alpha = np.asarray(rgba)[:, :, 3]
    coverage = float((alpha > ALPHA_THRESHOLD).mean())
    touches_edge = False
    if box is not None:
        left, top, right, bottom = box
        touches_edge = left == 0 or top == 0 or right == rgba.width or bottom == rgba.height
    return {
        "objectFound": box is not None,
        "coverage": round(coverage, 4),
        "touchesEdge": touches_edge,
    }


def fit_to_canvas(image: Image.Image, width: int, height: int, padding: float = 0.06) -> Image.Image:
    rgba = image.convert("RGBA")
    box = alpha_bbox(rgba)
    if box is None:
        raise ValueError("배경을 지우고 나니 남은 물체가 없습니다.")
    cropped = rgba.crop(box)
    inner_w = max(1.0, width * (1 - 2 * padding))
    inner_h = max(1.0, height * (1 - 2 * padding))
    scale = min(inner_w / cropped.width, inner_h / cropped.height)
    size = (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale)))
    resized = cropped.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(resized, ((width - size[0]) // 2, (height - size[1]) // 2))
    return canvas


def pixelate(image: Image.Image, size: int, colors: int) -> Image.Image:
    """Downscale so the longest side is ``size`` and limit the palette."""
    rgba = image.convert("RGBA")
    scale = size / max(rgba.width, rgba.height)
    target = (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale)))
    small = rgba.resize(target, Image.Resampling.BOX)
    pixels = np.asarray(small).copy()
    opaque = pixels[:, :, 3] >= 128
    if opaque.any():
        # 투명 칸의 검은 RGB가 팔레트 한 자리를 차지하지 않도록 물체 색으로 채운다.
        fill = np.median(pixels[opaque][:, :3], axis=0).astype(np.uint8)
        pixels[~opaque, :3] = fill
    rgb = Image.fromarray(pixels[:, :, :3], "RGB")
    quantized = rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    result = quantized.convert("RGBA")
    result.putalpha(Image.fromarray(np.where(opaque, 255, 0).astype(np.uint8), "L"))
    return result


def upscale_nearest(image: Image.Image, factor: int) -> Image.Image:
    return image.resize((image.width * factor, image.height * factor), Image.Resampling.NEAREST)
