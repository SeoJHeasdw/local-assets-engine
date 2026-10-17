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


def border_color(image: Image.Image) -> tuple[float, float, float]:
    """The median of the outermost pixels: the plain studio background an image model painted."""
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]))
    return tuple(float(v) for v in np.median(border, axis=0))


def decontaminate(image: Image.Image, background: tuple[float, float, float]) -> Image.Image:
    """Take the known background out of soft edges: color = (observed - (1 - alpha) * background) / alpha.

    Thin grass blades cut from a white backdrop otherwise keep a pale rim that shows on dark ground.
    """
    rgba = np.asarray(image.convert("RGBA"), dtype=np.float32) / 255.0
    alpha = rgba[..., 3:4]
    soft = (alpha > 0.02) & (alpha < 0.98)
    pure = (rgba[..., :3] - (1 - alpha) * np.asarray(background, dtype=np.float32)) / np.maximum(alpha, 0.02)
    rgba[..., :3] = np.where(soft, np.clip(pure, 0, 1), rgba[..., :3])
    return Image.fromarray((rgba * 255 + 0.5).astype(np.uint8), "RGBA")


def fill_transparent(image: Image.Image) -> Image.Image:
    """Give fully transparent pixels the color of the nearest opaque ones (push-pull), keeping alpha.

    Texture filtering and mipmaps blend in the color of transparent texels; black or white there makes
    a dark or light rim around alpha-tested foliage.
    """
    rgba = np.asarray(image.convert("RGBA"), dtype=np.float32) / 255.0
    weight = (rgba[..., 3] > 0.5).astype(np.float32)
    if not weight.any():
        return image.convert("RGBA")
    levels = [(rgba[..., :3] * weight[..., None], weight)]
    while min(levels[-1][1].shape) > 1:
        color, w = levels[-1]
        h, wd = (color.shape[0] + 1) // 2 * 2, (color.shape[1] + 1) // 2 * 2
        color = np.pad(color, ((0, h - color.shape[0]), (0, wd - color.shape[1]), (0, 0)))
        w = np.pad(w, ((0, h - w.shape[0]), (0, wd - w.shape[1])))
        levels.append((color.reshape(h // 2, 2, wd // 2, 2, 3).sum(axis=(1, 3)), w.reshape(h // 2, 2, wd // 2, 2).sum(axis=(1, 3))))
    filled = levels[-1][0] / np.maximum(levels[-1][1], 1e-6)[..., None]
    for color, w in reversed(levels[:-1]):
        up = np.repeat(np.repeat(filled, 2, axis=0), 2, axis=1)[: color.shape[0], : color.shape[1]]
        mean = color / np.maximum(w, 1e-6)[..., None]
        filled = np.where((w > 0)[..., None], mean, up)
    out = rgba.copy()
    clear = rgba[..., 3] <= 0.02
    out[clear, :3] = filled[clear]
    return Image.fromarray((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")


def fit_card(image: Image.Image, width: int, height: int, fill: float = 0.9,
             base_share: float = 0.04) -> tuple[Image.Image, dict[str, float]]:
    """A foliage card: the plant's base at the bottom edge's center, its height ``fill`` of the canvas.

    The base is the middle of the opaque pixels in the lowest ``base_share`` of the plant. If the plant
    is too wide to keep its base centered, it is made smaller and ``fill`` reports the height it got.
    """
    rgba = image.convert("RGBA")
    box = alpha_bbox(rgba, threshold=24)
    if box is None:
        raise ValueError("배경을 지우고 나니 남은 물체가 없습니다.")
    alpha = np.asarray(rgba, dtype=np.float32)[:, :, 3]
    left, top, right, bottom = box
    rows = max(1, round((bottom - top) * base_share))
    foot = alpha[bottom - rows:bottom, left:right]
    columns = np.arange(left, right, dtype=np.float32)
    base_x = float((foot.sum(axis=0) * columns).sum() / max(float(foot.sum()), 1e-6)) + 0.5
    scale = fill * height / (bottom - top)
    half = max(base_x - left, right - base_x)
    scale = min(scale, (width / 2 - 1) / half)
    size = (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale)))
    resized = rgba.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(resized, (round(width / 2 - base_x * scale), round(height - bottom * scale)))
    return canvas, {"fill": round((bottom - top) * scale / height, 4), "scale": round(scale, 4),
                    "baseX": round(base_x / rgba.width, 4)}


def grade_card(image: Image.Image, base_shade: float = 0.6, tip_warmth: float = 0.12) -> Image.Image:
    """Darker toward the base, lighter and yellower toward the tips, whatever shading the picture came with.

    Each row's median brightness (linear light, opaque pixels) is pulled toward a profile that rises from
    ``base_shade`` times the tips' brightness at the base to the tips' own brightness at the top. Image models
    paint grass stems pale, which reads lighter at the base than at the tips. Gains stay within 0.45–1.15 so a
    white flower head does not brighten the leaves below it.
    """
    rgba = np.asarray(image.convert("RGBA"), dtype=np.float32) / 255.0
    box = alpha_bbox(image, threshold=24)
    if box is None:
        return image.convert("RGBA")
    _, top, _, bottom = box
    height = max(bottom - top, 1)
    linear = np.where(rgba[..., :3] <= 0.04045, rgba[..., :3] / 12.92, ((rgba[..., :3] + 0.055) / 1.055) ** 2.4)
    lum = linear @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    rows = np.arange(rgba.shape[0], dtype=np.float32)
    t = np.clip((bottom - rows) / height, 0, 1)
    ease = t * t * (3 - 2 * t)
    solid = rgba[..., 3] > 0.5
    median = np.array([np.median(lum[row][solid[row]]) if solid[row].any() else np.nan for row in range(lum.shape[0])])
    known = ~np.isnan(median)
    tips = known & (t >= 0.6)
    gain = np.ones_like(t)
    if known.sum() >= 2 and tips.any():
        target = float(np.median(median[tips])) * (base_shade + (1 - base_shade) * ease)
        raw = np.clip(target[known] / np.maximum(median[known], 1e-4), 0.45, 1.15)
        gain = np.interp(rows, rows[known], raw)
        width = max(1, int(0.03 * height))
        kernel = np.exp(-0.5 * (np.arange(-3 * width, 3 * width + 1) / width) ** 2)
        gain = np.convolve(np.pad(gain, 3 * width, mode="edge"), kernel / kernel.sum(), mode="valid")
    warm = 1 + tip_warmth * ease[:, None, None] * np.array([0.6, 0.35, -0.9], dtype=np.float32)
    linear = np.clip(linear * gain[:, None, None] * warm, 0, 1)
    rgba[..., :3] = np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)
    return Image.fromarray((np.clip(rgba, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")
