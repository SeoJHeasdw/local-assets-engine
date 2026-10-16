"""Cheap image post-processing that runs inside the engine process."""

from __future__ import annotations

import numpy as np
from PIL import Image

ALPHA_THRESHOLD = 8
# 배경을 지운 그림이라고 믿으려면 이만큼은 투명하고, 테두리의 절반 이상이 투명해야 한다.
CUTOUT_MIN_TRANSPARENT = 0.01
CUTOUT_MIN_BORDER = 0.5
# 이보다 작은 물체는 우연히 남은 점일 수 있다(픽셀 수는 이미지 면적 대비, 변은 픽셀).
TINY_SUBJECT_FRACTION = 0.0005
TINY_SUBJECT_PIXELS = 64
TINY_SUBJECT_SIDE = 8
# 캔버스에 맞추며 이보다 크게 키우면 원본에 없는 크기로 부풀린 것이다.
OVERSCALE = 8.0


def has_transparency(image: Image.Image) -> bool:
    if image.mode not in ("RGBA", "LA", "PA") and "transparency" not in image.info:
        return False
    alpha = np.asarray(image.convert("RGBA"))[:, :, 3]
    return bool((alpha < 255).any())


def has_cutout(image: Image.Image) -> bool:
    """Whether the alpha channel is an actual cutout, not a few stray translucent pixels."""
    if not has_transparency(image):
        return False
    alpha = np.asarray(image.convert("RGBA"))[:, :, 3]
    border = np.concatenate((alpha[0], alpha[-1], alpha[:, 0], alpha[:, -1]))
    return bool((alpha < 128).mean() >= CUTOUT_MIN_TRANSPARENT and (border < 128).mean() >= CUTOUT_MIN_BORDER)


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
    pixels = int((alpha > ALPHA_THRESHOLD).sum())
    touches_edge = tiny = False
    if box is not None:
        left, top, right, bottom = box
        touches_edge = left == 0 or top == 0 or right == rgba.width or bottom == rgba.height
        tiny = (pixels < max(TINY_SUBJECT_PIXELS, TINY_SUBJECT_FRACTION * alpha.size)
                or min(right - left, bottom - top) < TINY_SUBJECT_SIDE)
    return {
        "objectFound": box is not None,
        "coverage": round(pixels / alpha.size, 4),
        "pixels": pixels,
        "touchesEdge": touches_edge,
        "tiny": tiny,
    }


def canvas_scale(image: Image.Image, width: int, height: int, padding: float = 0.06) -> float | None:
    """How much ``fit_to_canvas`` enlarges (or shrinks) the object."""
    box = alpha_bbox(image)
    if box is None:
        return None
    inner_w, inner_h = max(1.0, width * (1 - 2 * padding)), max(1.0, height * (1 - 2 * padding))
    return min(inner_w / (box[2] - box[0]), inner_h / (box[3] - box[1]))


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


def cover(image: Image.Image, width: int, height: int,
          background: tuple[int, int, int] = (255, 255, 255)) -> tuple[Image.Image, dict[str, int]]:
    """Center-crop to the target aspect, then resize: an opaque RGB frame of exactly
    width×height and the crop box in source pixels. Stretching would distort
    what the video model continues from; transparent pixels sit on ``background``."""
    rgba = image.convert("RGBA")
    flat = Image.new("RGBA", rgba.size, (*background, 255))
    flat.alpha_composite(rgba)
    scale = max(width / flat.width, height / flat.height)
    crop_w = min(flat.width, round(width / scale))
    crop_h = min(flat.height, round(height / scale))
    left, top = (flat.width - crop_w) // 2, (flat.height - crop_h) // 2
    box = {"x": left, "y": top, "width": crop_w, "height": crop_h}
    framed = flat.crop((left, top, left + crop_w, top + crop_h)).convert("RGB")
    return framed.resize((width, height), Image.Resampling.LANCZOS), box


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
