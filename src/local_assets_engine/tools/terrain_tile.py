"""A seamless Unity terrain layer from CC0 scans.

Each scan is resampled so a pixel covers the same ground as in the output tile, then, unless the scan
already spans the tile, quilted into a new tile whose period is the requested real size: square patches
from inside the scan (not across its edge), laid in a grid that wraps at every edge and joined along the
cheapest seam through each overlap (image quilting, Efros & Freeman 2001, with graph-cut seams, Kwatra et al. 2003). The scan's own repeat does not show
inside the tile. Patches holding the scan's outliers (one large stone, a puddle: smoothed brightness or
height beyond 3σ) are passed over. A second scan can show through the first in noise-shaped patches,
by height, for an exact share of the area (the meadow's soil between grass).

Light: the brightness that follows the normal map's x and y (a sun direction baked into the photo) is
divided out, large blotches are evened by ``flatten``, and an optional grade turns greens toward yellow.

Maps (``size``², seamless):
``T_<name>_basecolor.png``  sRGB
``T_<name>_normal.png``     tangent space, OpenGL +Y
``T_<name>_mask.png``       Unity terrain mask map: R metallic 0, G occlusion, B height, A smoothness
plus ``preview.png`` (3×3 tiles lit from the upper left, and the maps) and ``tile.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from . import progress_line

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
# 매칭 특징의 가중치: 선형 RGB, 높이, 노멀 x·y.
FEATURE_WEIGHTS = np.array([1, 1, 1, 1, 0.5, 0.5], dtype=np.float32)
CANDIDATES = 192
# 최소 오차의 이 비율 안에 드는 후보 중 하나를 무작위로 고른다(같은 조각만 반복되지 않게).
TOLERANCE = 0.12
OUTLIER_SIGMA = 3.0
OUTLIER_METERS = 0.04
FEATHER_PX = 1.5
# 이음 자리를 찾는 그래프의 겹침 폭(픽셀). 겹침이 이보다 넓으면 블록으로 묶어 푼다.
CUT_OVERLAP_PX = 32
BLOCKED = 1e9
# 두 질감을 섞을 때: 높이 차이를 노이즈에 더하는 정도와 경계 폭(표준편차 단위).
BLEND_HEIGHT = 1.0
BLEND_WIDTH = 0.18
# 형광 연두로 보는 범위: 색상(도), 채도, 밝기.
NEON = ((75.0, 140.0), 0.55, 0.45)


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055).astype(np.float32)


def rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    value = rgb.max(axis=-1)
    chroma = value - rgb.min(axis=-1)
    safe = np.where(chroma > 1e-6, chroma, 1.0)
    hue = np.where(value == r, ((g - b) / safe) % 6, np.where(value == g, (b - r) / safe + 2, (r - g) / safe + 4))
    hue = np.where(chroma > 1e-6, hue * 60.0, 0.0)
    saturation = np.where(value > 1e-6, chroma / np.where(value > 1e-6, value, 1.0), 0.0)
    return np.stack([hue, saturation, value], axis=-1).astype(np.float32)


def hsv_to_rgb(hsv: np.ndarray) -> np.ndarray:
    hue, saturation, value = hsv[..., 0] % 360.0, hsv[..., 1], hsv[..., 2]
    chroma = value * saturation
    x = chroma * (1 - np.abs((hue / 60.0) % 2 - 1))
    m = value - chroma
    sector = (hue // 60).astype(int) % 6
    zero = np.zeros_like(hue)
    table = [(chroma, x, zero), (x, chroma, zero), (zero, chroma, x), (zero, x, chroma), (x, zero, chroma), (chroma, zero, x)]
    rgb = np.zeros(hue.shape + (3,), dtype=np.float32)
    for index, channels in enumerate(table):
        pick = sector == index
        for channel, values in enumerate(channels):
            rgb[..., channel][pick] = values[pick]
    return rgb + m[..., None]


def resample_periodic(array: np.ndarray, size: int) -> np.ndarray:
    """Resize a seamless square map to ``size``² pixels, wrapping at the edges, antialiased when shrinking."""
    from scipy.ndimage import gaussian_filter, zoom

    source = array.shape[0]
    if source == size:
        return array.astype(np.float32)
    factor = size / source
    extra = [0] * (array.ndim - 2)
    if factor < 1:
        array = gaussian_filter(array, [0.45 / factor] * 2 + extra, mode="wrap")
    resized = zoom(array, [factor, factor] + [1] * (array.ndim - 2), order=3, mode="grid-wrap", grid_mode=True)
    return resized.astype(np.float32)


def _read(path: Path, mode: str) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as image:
        return np.asarray(image.convert(mode), dtype=np.float32) / 255.0


def load_scan(folder: Path, size: int) -> dict[str, np.ndarray]:
    """A texture scan's maps at ``size``² pixels: linear color, unit normals, roughness, occlusion, height."""
    maps = {
        "color": srgb_to_linear(resample_periodic(_read(folder / "diff.jpg", "RGB"), size)),
        "normal": resample_periodic(_read(folder / "nor_gl.jpg", "RGB") * 2 - 1, size),
        "rough": resample_periodic(_read(folder / "rough.jpg", "L"), size),
        "ao": resample_periodic(_read(folder / "ao.jpg", "L"), size),
        "height": resample_periodic(_read(folder / "disp.jpg", "L"), size),
    }
    maps["normal"] /= np.linalg.norm(maps["normal"], axis=-1, keepdims=True).clip(1e-6)
    for key in ("rough", "ao", "height"):
        maps[key] = np.clip(maps[key], 0.0, 1.0)
    return maps


def luminance(color: np.ndarray) -> np.ndarray:
    return color @ LUMA


def directional_light(color: np.ndarray, normal: np.ndarray) -> tuple[float, float]:
    """How much brightness follows the normal's x and y, relative to the mean: a baked sun direction."""
    lum = luminance(color).ravel().astype(np.float64)
    nx, ny = normal[..., 0].ravel().astype(np.float64), normal[..., 1].ravel().astype(np.float64)
    design = np.stack([np.ones_like(nx), nx, ny], axis=1)
    (a, b, c), *_ = np.linalg.lstsq(design[::7], lum[::7], rcond=None)
    return (float(b / a), float(c / a)) if a > 1e-6 else (0.0, 0.0)


def remove_directional(maps: dict[str, np.ndarray], slope: tuple[float, float], rounds: int = 3) -> tuple[float, float]:
    """Divide the baked sun out of the color, measuring again after each pass; returns what is left.

    A low sun on a stony scan (slope ~1.1) needs gains well past ±30%; each pass is clamped to 0.5–2.0.
    """
    normal = maps["normal"]
    for _ in range(rounds):
        gain = 1.0 / np.clip(1 + slope[0] * normal[..., 0] + slope[1] * normal[..., 1], 0.5, 2.0)
        maps["color"] *= gain[..., None]
        slope = directional_light(maps["color"], normal)
    return slope


def features(maps: dict[str, np.ndarray]) -> np.ndarray:
    stack = np.concatenate([maps["color"], maps["height"][..., None], maps["normal"][..., :2]], axis=-1)
    scale = stack.reshape(-1, stack.shape[-1])[::5].std(axis=0).clip(1e-4)
    return (stack / scale * FEATURE_WEIGHTS).astype(np.float32)


def outlier_map(maps: dict[str, np.ndarray], px_per_meter: float) -> np.ndarray:
    """Pixels inside a stand-out feature: smoothed brightness or height beyond ``OUTLIER_SIGMA``."""
    from scipy.ndimage import gaussian_filter

    sigma = max(1.0, OUTLIER_METERS * px_per_meter)
    marked = np.zeros(maps["height"].shape, dtype=bool)
    for values in (luminance(maps["color"]), maps["height"]):
        smooth = gaussian_filter(values, sigma, mode="reflect")
        marked |= np.abs(smooth - smooth.mean()) > OUTLIER_SIGMA * max(float(smooth.std()), 1e-6)
    return marked


def grid(size: int, patch_px: float) -> tuple[int, int]:
    """Patches per row (a divisor of ``size``, so the grid wraps exactly) and the step between them."""
    target = patch_px * 0.75
    counts = [count for count in range(2, 129) if size % count == 0 and size // count >= 24]
    count = min(counts, key=lambda c: abs(math.log((size / c) / target)))
    return count, size // count


def patch_offsets(rng: np.random.Generator, period: int, patch: int, count: int, margin: int = 4) -> np.ndarray:
    """Top-left corners of candidate patches that stay inside the scan.

    Scans sold as seamless still step at their own edge (``forest_leaves_02`` 1.4–1.7 times an ordinary
    neighbor step), and a patch taken across it would carry that line into the tile. Too small a scan wraps.
    """
    room = period - patch - 2 * margin
    if room <= 0:
        return rng.integers(0, period, size=(count, 2))
    return margin + rng.integers(0, room + 1, size=(count, 2))


def graph_cut(cost: np.ndarray, known: np.ndarray, pinned: np.ndarray) -> np.ndarray:
    """Which pixels of a patch take the new patch: a minimum cut between what is there and the new patch.

    ``cost`` is how much the two differ at each pixel; cutting between neighbors ``s`` and ``t`` costs
    ``cost[s] + cost[t]`` (Kwatra et al. 2003). Unfilled pixels and the filled pixels next to them take the new
    patch, and ``pinned`` filled pixels (on the patch's edge, next to filled pixels outside it) keep what is
    there, so the seam runs inside the overlap and the patch never ends flush against filled pixels — not at an
    L-shaped corner and not across the tile's wrap.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import breadth_first_order, maximum_flow

    take = np.ones(known.shape, dtype=bool)
    if not (known & pinned).any():
        return take
    index = np.full(known.shape, -1, dtype=np.int64)
    index[known] = np.arange(int(known.sum()))
    nodes = int(known.sum())
    source, sink = nodes, nodes + 1
    weight = np.round(cost * 100).astype(np.int64) + 1
    rows, cols, caps = [], [], []
    for axis in (0, 1):
        a = (slice(None, -1), slice(None)) if axis == 0 else (slice(None), slice(None, -1))
        b = (slice(1, None), slice(None)) if axis == 0 else (slice(None), slice(1, None))
        both = known[a] & known[b]
        edge = (weight[a] + weight[b])[both]
        ia, ib = index[a][both], index[b][both]
        rows += [ia, ib]
        cols += [ib, ia]
        caps += [edge, edge]
    # A filled pixel next to an unfilled one takes the new patch too, so the seam runs inside the overlap and
    # not along its edge. Where a pixel is both (an overlap one pixel wide), keeping what is there wins.
    edge_of_new = np.zeros(known.shape, dtype=bool)
    edge_of_new[:-1] |= known[:-1] & ~known[1:]
    edge_of_new[1:] |= known[1:] & ~known[:-1]
    edge_of_new[:, :-1] |= known[:, :-1] & ~known[:, 1:]
    edge_of_new[:, 1:] |= known[:, 1:] & ~known[:, :-1]
    freed = index[edge_of_new & ~pinned]
    rows.append(freed)
    cols.append(np.full(freed.size, sink))
    caps.append(np.full(freed.size, 1 << 28))
    held = index[known & pinned]
    rows.append(np.full(held.size, source))
    cols.append(held)
    caps.append(np.full(held.size, 1 << 28))
    rows, cols, caps = np.concatenate(rows), np.concatenate(cols), np.concatenate(caps)
    capacity = coo_matrix((caps.astype(np.int32), (rows, cols)), shape=(nodes + 2, nodes + 2)).tocsr()
    capacity.sum_duplicates()
    flow = maximum_flow(capacity, source, sink).flow
    residual = (capacity - flow).tocsr()
    residual.data[residual.data < 0] = 0
    residual.eliminate_zeros()
    kept = breadth_first_order(residual, source, directed=True, return_predecessors=False)
    old = np.zeros(nodes + 2, dtype=bool)
    old[kept] = True
    take[known] = ~old[:nodes]
    return take


def coarse_cut(cost: np.ndarray, known: np.ndarray, pinned: np.ndarray, factor: int) -> np.ndarray:
    """``graph_cut`` on ``factor``-pixel blocks, then back to full size with the constraints kept exact.

    The flow takes far longer as the overlap widens; a seam placed to a few pixels is enough once feathered.
    A block is filled only if all its pixels are, and pinned if any is.
    """
    if factor <= 1:
        return graph_cut(cost, known, pinned)
    size = cost.shape[0]
    blocks = size // factor
    crop = (slice(0, blocks * factor),) * 2

    def reduce(values, how):
        return how(values[crop].reshape(blocks, factor, blocks, factor), axis=(1, 3))

    take = graph_cut(reduce(cost, np.mean), reduce(known, np.all), reduce(pinned & known, np.any) & reduce(known, np.all))
    full = np.repeat(np.repeat(take, factor, axis=0), factor, axis=1)
    full = np.pad(full, ((0, size - full.shape[0]), (0, size - full.shape[1])), mode="edge")
    full |= ~known
    full[pinned & known] = False
    return full


def quilt(source: dict[str, np.ndarray], size: int, patch_px: float, px_per_meter: float,
          rng: np.random.Generator, report=None) -> tuple[dict[str, np.ndarray], dict]:
    """A ``size``² tile with period ``size`` made of patches of the periodic ``source`` maps."""
    from scipy.ndimage import gaussian_filter

    period = source["height"].shape[0]
    count, step = grid(size, patch_px)
    overlap = max(4, step // 3)
    patch = step + overlap
    feats = features(source)
    marked = outlier_map(source, px_per_meter)
    out = {key: np.zeros((size, size) + value.shape[2:], dtype=np.float32) for key, value in source.items()}
    out_feats = np.zeros((size, size, feats.shape[-1]), dtype=np.float32)
    filled = np.zeros((size, size), dtype=bool)
    sub = max(1, overlap // 12)
    probe = np.arange(0, patch, max(4, patch // 40))
    errors, refused = [], 0
    for i in range(count):
        for j in range(count):
            ys, xs = (i * step + np.arange(patch)) % size, (j * step + np.arange(patch)) % size
            region = np.ix_(ys, xs)
            known = filled[region]
            offsets = patch_offsets(rng, period, patch, CANDIDATES)
            ly, lx = np.nonzero(known[::sub, ::sub])
            ly, lx = ly * sub, lx * sub
            if ly.size:
                target = out_feats[ys[ly], xs[lx]]
                sy = (offsets[:, 0, None] + ly[None]) % period
                sx = (offsets[:, 1, None] + lx[None]) % period
                error = ((feats[sy, sx] - target[None]) ** 2).sum(axis=-1).mean(axis=-1)
            else:
                error = np.zeros(CANDIDATES)
            oy = (offsets[:, 0, None, None] + probe[None, :, None]) % period
            ox = (offsets[:, 1, None, None] + probe[None, None, :]) % period
            outliers = marked[oy, ox].sum(axis=(1, 2))
            refused += int((outliers > 0).sum())
            ranked = error + outliers * BLOCKED
            best = float(ranked.min())
            pool = np.nonzero(ranked <= best * (1 + TOLERANCE) + 1e-9)[0]
            pick = int(rng.choice(pool))
            errors.append(float(error[pick]))
            py, px = (offsets[pick, 0] + np.arange(patch)) % period, (offsets[pick, 1] + np.arange(patch)) % period
            piece = np.ix_(py, px)
            new_feats = feats[piece]
            take = np.ones((patch, patch), dtype=bool)
            if known.any():
                # Filled pixels on the patch's edge that touch filled pixels just outside it stay as they are.
                pinned = np.zeros((patch, patch), dtype=bool)
                pinned[0] = filled[(ys[0] - 1) % size, xs]
                pinned[-1] |= filled[(ys[-1] + 1) % size, xs]
                pinned[:, 0] |= filled[ys, (xs[0] - 1) % size]
                pinned[:, -1] |= filled[ys, (xs[-1] + 1) % size]
                difference = np.sqrt(((new_feats - out_feats[region]) ** 2).sum(axis=-1))
                take = coarse_cut(difference, known, pinned, max(1, overlap // CUT_OVERLAP_PX))
            alpha = gaussian_filter(take.astype(np.float32), FEATHER_PX, mode="nearest")
            alpha[~known] = 1.0
            for key, value in source.items():
                old, new = out[key][region], value[piece]
                weight = alpha if new.ndim == 2 else alpha[..., None]
                out[key][region] = old * (1 - weight) + new * weight
            out_feats[region] = out_feats[region] * (1 - alpha[..., None]) + new_feats * alpha[..., None]
            filled[region] = True
        if report:
            report(i + 1, count)
    out["normal"] /= np.linalg.norm(out["normal"], axis=-1, keepdims=True).clip(1e-6)
    stats = {"patchesPerRow": count, "stepPx": step, "overlapPx": overlap, "patchMeters": round(patch / px_per_meter, 3),
             "sourceOutlierFraction": round(float(marked.mean()), 5), "candidatesRefusedForOutliers": refused,
             "meanCutError": round(float(np.mean(errors)), 4)}
    return out, stats


def height_gradients(height: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Forward differences of a scan's height; the last row and column (never inside a patch) are zero."""
    gx, gy = np.zeros_like(height), np.zeros_like(height)
    gx[:, :-1] = height[:, 1:] - height[:, :-1]
    gy[:-1] = height[1:] - height[:-1]
    return gx, gy


def integrate_periodic(gx: np.ndarray, gy: np.ndarray) -> np.ndarray:
    """The periodic height whose forward differences come closest to ``gx``, ``gy`` (Poisson, solved by FFT).

    Quilting heights directly leaves each patch at its own level, a grid of blocks in the mask's height; the
    slopes join cleanly across the seams, and one solve over the whole tile puts every patch on one surface.
    """
    size_y, size_x = gx.shape
    divergence = (gx - np.roll(gx, 1, axis=1)) + (gy - np.roll(gy, 1, axis=0))
    u = np.fft.fftfreq(size_x)[None, :]
    v = np.fft.fftfreq(size_y)[:, None]
    eigen = 2 * np.cos(2 * np.pi * u) + 2 * np.cos(2 * np.pi * v) - 4
    eigen[0, 0] = 1.0
    spectrum = np.fft.fft2(divergence) / eigen
    spectrum[0, 0] = 0.0
    return np.real(np.fft.ifft2(spectrum)).astype(np.float32)


def periodic_noise(size: int, wavelength_px: float, rng: np.random.Generator) -> np.ndarray:
    """Zero-mean, unit-variance noise with features near ``wavelength_px``, periodic over ``size``."""
    fy = np.fft.fftfreq(size)[:, None]
    fx = np.fft.rfftfreq(size)[None, :]
    frequency = np.hypot(fy, fx)
    field = np.zeros((size, size), dtype=np.float64)
    for octave, weight in ((1.0, 1.0), (2.5, 0.45), (6.0, 0.2)):
        center = octave / wavelength_px
        band = np.exp(-0.5 * (np.log(np.maximum(frequency, 1e-9) / center) / 0.5) ** 2)
        band[0, 0] = 0.0
        layer = np.fft.irfft2(np.fft.rfft2(rng.standard_normal((size, size))) * band, s=(size, size))
        field += weight * layer / max(float(layer.std()), 1e-12)
    return ((field - field.mean()) / field.std()).astype(np.float32)


def _smoothstep(low, high, values):
    t = np.clip((values - low) / (high - low), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def blend_weight(base_height: np.ndarray, patch_height: np.ndarray, noise: np.ndarray, coverage: float) -> np.ndarray:
    """Where the second layer shows, as a weight whose mean is ``coverage``. Higher patch ground wins first."""
    def z(values):
        return (values - values.mean()) / max(float(values.std()), 1e-6)

    score = noise + BLEND_HEIGHT * (z(patch_height) - z(base_height))
    low, high = float(score.min()) - 1, float(score.max()) + 1
    for _ in range(40):
        middle = (low + high) / 2
        share = float(_smoothstep(middle - BLEND_WIDTH, middle + BLEND_WIDTH, score).mean())
        low, high = (middle, high) if share > coverage else (low, middle)
    threshold = (low + high) / 2
    return _smoothstep(threshold - BLEND_WIDTH, threshold + BLEND_WIDTH, score).astype(np.float32)


def blend(base: dict[str, np.ndarray], patch: dict[str, np.ndarray], weight: np.ndarray) -> dict[str, np.ndarray]:
    def unit(values):
        low, high = np.percentile(values, (0.5, 99.5))
        return np.clip((values - low) / max(high - low, 1e-6), 0, 1)

    w = weight[..., None]
    mixed = {key: base[key] * (1 - (w if base[key].ndim == 3 else weight)) +
             patch[key] * (w if patch[key].ndim == 3 else weight) for key in ("color", "normal", "rough", "ao")}
    mixed["height"] = unit(base["height"]) * (1 - weight) + unit(patch["height"]) * weight
    mixed["normal"] /= np.linalg.norm(mixed["normal"], axis=-1, keepdims=True).clip(1e-6)
    return mixed


def flatten(maps: dict[str, np.ndarray], strength: float, sigma_px: float) -> dict:
    """Even out brightness and height blotches larger than ``sigma_px``, which give a tile's repeat away."""
    from scipy.ndimage import gaussian_filter

    lum = luminance(maps["color"])
    smooth = gaussian_filter(lum, sigma_px, mode="wrap")
    before = float(smooth.std() / lum.mean())
    maps["color"] *= np.clip((lum.mean() / smooth.clip(1e-4)) ** strength, 0.6, 1.6)[..., None]
    # 높이는 Unity 레이어 섞기에 쓰는 가까운 요철이다. 얼룩보다 큰 기복은 지형 형상이 맡으므로 모두 뺀다.
    height_smooth = gaussian_filter(maps["height"], sigma_px, mode="wrap")
    maps["height"] = maps["height"] - (height_smooth - height_smooth.mean())
    after = float(gaussian_filter(luminance(maps["color"]), sigma_px, mode="wrap").std() / lum.mean())
    return {"strength": strength, "sigmaPx": round(sigma_px, 1), "blotchBefore": round(before, 4),
            "blotchAfter": round(after, 4)}


def grade(color: np.ndarray, hue_shift: float = 0.0, saturation: float = 1.0, value: float = 1.0,
          max_saturation: float | None = None) -> np.ndarray:
    """Linear color graded in HSV: greens turn ``hue_shift``° toward yellow, saturation is scaled and capped."""
    hsv = rgb_to_hsv(linear_to_srgb(color))
    if hue_shift:
        green = np.clip(1 - np.abs(hsv[..., 0] - 100.0) / 55.0, 0, 1)
        hsv[..., 0] -= hue_shift * green
    hsv[..., 1] *= saturation
    if max_saturation:
        knee = 0.8 * max_saturation
        over = hsv[..., 1] > knee
        hsv[..., 1][over] = knee + (max_saturation - knee) * np.tanh((hsv[..., 1][over] - knee) / (max_saturation - knee))
    hsv[..., 2] *= value
    return srgb_to_linear(np.clip(hsv_to_rgb(np.clip(hsv, 0, None)), 0, 1))


def neon_fraction(color: np.ndarray) -> float:
    hsv = rgb_to_hsv(linear_to_srgb(color))
    (low, high), saturation, value = NEON
    return float(((hsv[..., 0] >= low) & (hsv[..., 0] <= high) & (hsv[..., 1] > saturation) & (hsv[..., 2] > value)).mean())


def wrap_seam_ratio(values: np.ndarray) -> float:
    """Step across the tile's wrap edge relative to an ordinary step between neighbors (1 = no seam)."""
    inner = (np.abs(np.diff(values, axis=1)).mean() + np.abs(np.diff(values, axis=0)).mean()) / 2
    wrap = (np.abs(values[:, 0] - values[:, -1]).mean() + np.abs(values[0] - values[-1]).mean()) / 2
    return float(wrap / max(inner, 1e-6))


def build(spec: dict, report=None) -> tuple[dict[str, np.ndarray], dict]:
    size, tile = int(spec["size"]), float(spec["tileMeters"])
    px_per_meter = size / tile
    rng = np.random.default_rng(int(spec["seed"]))
    stats: dict = {"pxPerMeter": round(px_per_meter, 2), "layers": []}

    layers = [key for key in ("layer", "patch") if spec.get(key)]
    share = 0.85 / len(layers)

    def layer(source: dict, label: str, start: float) -> dict[str, np.ndarray]:
        if report:
            report(start, f"{label} 스캔 불러오는 중")
        scan_px = max(16, round(float(source["sizeMeters"]) * px_per_meter))
        maps = load_scan(Path(source["dir"]), scan_px)
        slope = directional_light(maps["color"], maps["normal"])
        left = remove_directional(maps, slope)
        if source.get("roughness"):
            # 스캔의 5~95% 거칠기를 이 범위로 옮긴다(예: 반들거리게 만든 절차적 풀).
            low, high = np.percentile(maps["rough"], (5, 95))
            target = source["roughness"]
            maps["rough"] = np.clip(target[0] + (maps["rough"] - low) / max(high - low, 1e-6) * (target[1] - target[0]), 0, 1)
        entry = {"id": source["id"], "sizeMeters": source["sizeMeters"], "scanPx": scan_px,
                 "directionalLight": [round(slope[0], 4), round(slope[1], 4)],
                 "directionalLightAfter": [round(v, 4) for v in left]}
        # 스캔 높이의 큰 기복을 먼저 뺀다. 남기면 기울기가 다른 곳에서 온 조각들이 높이에서 계단처럼 어긋난다.
        from scipy.ndimage import gaussian_filter

        # 조각은 스캔 안쪽에서만 뽑으므로 스캔을 감지 않는다(이음매 없다는 스캔도 가장자리에서 끊긴다).
        maps["gx"], maps["gy"] = height_gradients(maps["height"])
        # 조각 고르기에 쓰는 높이는 완만한 기복을 뺀다. 결과 높이는 이은 기울기를 적분해 얻는다.
        relief = min(float(spec["flattenMeters"]), float(spec["patchMeters"]) / 2) * px_per_meter
        maps["height"] = maps["height"] - gaussian_filter(maps["height"], relief, mode="reflect")
        # 스캔이 타일과 같은 크기여도 잇는다. 그대로 쓰면 스캔의 튀는 곳(큰 이끼 덩이, 긴 잔가지)이 타일마다 반복된다.
        maps, entry["quilt"] = quilt(maps, size, float(spec["patchMeters"]) * px_per_meter, px_per_meter, rng,
                                     report=(lambda done, total: report(start + share * done / total,
                                                                        f"{label} 조각 잇는 중 {done}/{total}"))
                                     if report else None)
        maps["height"] = integrate_periodic(maps.pop("gx"), maps.pop("gy"))
        stats["layers"].append(entry)
        return maps

    maps = layer(spec["layer"], "바탕", 0.0)
    if spec.get("patch"):
        patch_spec = spec["patch"]
        second = layer(patch_spec, "드러나는 땅", share)
        noise = periodic_noise(size, float(patch_spec["noiseMeters"]) * px_per_meter, rng)
        weight = blend_weight(maps["height"], second["height"], noise, float(patch_spec["coverage"]))
        maps = blend(maps, second, weight)
        stats["patchCoverage"] = round(float(weight.mean()), 4)
    if report:
        report(0.88, "얼룩 고르게·색 보정 중")
    stats["flatten"] = flatten(maps, float(spec["flatten"]), float(spec["flattenMeters"]) * px_per_meter)
    graded = spec.get("grade") or {}
    stats["neonBefore"] = round(neon_fraction(maps["color"]), 5)
    if graded:
        maps["color"] = grade(maps["color"], graded.get("hueShift", 0.0), graded.get("saturation", 1.0),
                              graded.get("value", 1.0), graded.get("maxSaturation"))
    stats["grade"] = graded
    stats["neonFraction"] = round(neon_fraction(maps["color"]), 5)
    lum = luminance(maps["color"])
    stats["wrapSeamRatio"] = round(max(wrap_seam_ratio(lum), wrap_seam_ratio(maps["height"])), 3)
    stats["meanBaseColorSrgb"] = [round(float(v), 4) for v in linear_to_srgb(maps["color"]).reshape(-1, 3).mean(axis=0)]
    return maps, stats


def pack(maps: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """8-bit images: sRGB base color, OpenGL normal, Unity terrain mask (R 0, G occlusion, B height, A smoothness)."""
    def byte(values):
        return (np.clip(values, 0, 1) * 255 + 0.5).astype(np.uint8)

    low, high = np.percentile(maps["height"], (0.5, 99.5))
    height = np.clip((maps["height"] - low) / max(high - low, 1e-6), 0, 1)
    mask = np.stack([np.zeros_like(height), maps["ao"], height, 1 - maps["rough"]], axis=-1)
    return {"basecolor": byte(linear_to_srgb(maps["color"])), "normal": byte(maps["normal"] * 0.5 + 0.5),
            "mask": byte(mask)}


def preview(images: dict[str, np.ndarray], title: str, tile_px: int = 400, map_px: int = 240):
    """3×3 tiles lit from the upper left (repeat and seams show here) beside the separate maps."""
    from PIL import Image, ImageDraw

    def small(array, px):
        return np.asarray(Image.fromarray(array).resize((px, px), Image.LANCZOS), dtype=np.float32) / 255.0

    color, normal = small(images["basecolor"], tile_px), small(images["normal"], tile_px) * 2 - 1
    light = np.array([-0.45, 0.45, 0.77], dtype=np.float32)
    light /= np.linalg.norm(light)
    shade = 0.45 + 0.75 * np.clip(normal @ light, 0, 1)
    lit = (np.clip(color * shade[..., None], 0, 1) * 255).astype(np.uint8)
    tiled = np.tile(lit, (3, 3, 1))
    mask = images["mask"]
    panels = [("basecolor", images["basecolor"]), ("normal", images["normal"]),
              ("G occlusion", np.repeat(mask[..., 1:2], 3, axis=-1)), ("B height", np.repeat(mask[..., 2:3], 3, axis=-1)),
              ("A smoothness", np.repeat(mask[..., 3:4], 3, axis=-1))]
    board = Image.new("RGB", (tile_px * 3 + map_px, max(tile_px * 3, map_px * len(panels)) + 28), (36, 38, 42))
    board.paste(Image.fromarray(tiled), (0, 28))
    draw = ImageDraw.Draw(board)
    draw.text((8, 8), title, fill=(235, 235, 235))
    for index, (label, array) in enumerate(panels):
        y = 28 + index * map_px
        board.paste(Image.fromarray(array).resize((map_px, map_px), Image.LANCZOS), (tile_px * 3, y))
        draw.text((tile_px * 3 + 6, y + 4), label, fill=(255, 255, 255))
    return board


def main(argv: list[str] | None = None) -> int:
    from PIL import Image

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text("utf-8"))
    started = time.monotonic()

    def report(fraction: float, detail: str) -> None:
        print(progress_line(round(fraction * 1000), 1000, detail), flush=True)

    maps, stats = build(spec, report)
    report(0.94, "맵 묶고 저장하는 중")
    images = pack(maps)
    args.output.mkdir(parents=True, exist_ok=True)
    name = spec["name"]
    files = {}
    for key, array in images.items():
        files[key] = f"T_{name}_{key}.png"
        Image.fromarray(array).save(args.output / files[key])
    files["preview"] = "preview.png"
    preview(images, f"T_{name} · {spec['tileMeters']} m · {spec['size']} px").save(args.output / files["preview"])
    stats.update({"name": name, "tileMeters": spec["tileMeters"], "size": spec["size"], "seed": spec["seed"],
                  "files": files, "seconds": round(time.monotonic() - started, 2)})
    (args.output / "tile.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")
    report(1.0, f"완료 · 이음매 비 {stats['wrapSeamRatio']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
