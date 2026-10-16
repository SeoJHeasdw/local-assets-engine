"""One trim-sheet atlas for a modular building kit, composed from CC0 scanned wood.

Every region repeats across the full atlas width, so a board's U runs along its length and wraps;
its V stays inside one region. The atlas is twice as tall as wide:

``planks``    the plank wall scan itself (one tile). Each board of the kit maps onto one of its
              plank rows, found from the height map's seams.
``timber``    rough-sawn wood turned so the grain runs along U and graded to the planks' color,
              for posts, beams, rafters and stringers.
``shingles``  the same rough wood in its own weathered tone, cut into bands of shingles with
              painted joints, for roof rows.

Maps: base color (sRGB), normal (tangent space, OpenGL +Y), roughness, occlusion, and the
Unity URP metallic-smoothness pack (R metallic 0, G occlusion, A smoothness). ``layout.json``
gives the regions, rows and bands in UV (V up) and the meters one U unit spans.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from . import progress_line

MAPS = ("diff", "nor_gl", "rough", "ao", "disp")
# 너와 한 장의 폭(m) 범위와 이음 틈의 폭(m).
SHINGLE_WIDTH = (0.11, 0.26)
SHINGLE_JOINT = 0.006
SHINGLE_BANDS = 4


def _load(folder: Path, name: str, size: tuple[int, int]) -> np.ndarray:
    from PIL import Image

    with Image.open(folder / f"{name}.jpg") as image:
        mode = "RGB" if name in ("diff", "nor_gl") else "L"
        resized = image.convert(mode).resize(size, Image.LANCZOS)
    return np.asarray(resized, dtype=np.float32) / 255.0


def load_material(folder: Path, size: int) -> dict[str, np.ndarray]:
    return {name: _load(folder, name, (size, size)) for name in MAPS}


def plank_rows(height: np.ndarray, rows_expected: tuple[int, int] = (8, 40)) -> list[tuple[int, int]]:
    """Pixel rows ``(top, bottom)`` of each plank, between the gaps of a horizontal plank scan.

    A gap is where the height map is lowest across the width; the row that wraps over the
    tile's top and bottom edge is left out because it is not contiguous in the atlas.
    """
    from scipy.ndimage import gaussian_filter1d
    from scipy.signal import find_peaks

    size = height.shape[0]
    low = (height <= np.percentile(height, 4)).mean(axis=1)
    profile = gaussian_filter1d(low, max(1.0, size / 1400))
    seams, _ = find_peaks(profile, distance=max(4, size // 40), prominence=0.05)
    if not rows_expected[0] <= len(seams) <= rows_expected[1]:
        raise SystemExit(f"판자 줄을 {len(seams)}개 찾았습니다. 가로 판자 스캔인지 확인하세요.")
    return [(int(top), int(bottom)) for top, bottom in zip(seams[:-1], seams[1:])]


def remove_joints(maps: dict[str, np.ndarray], rows: list[tuple[int, int]]) -> int:
    """Close the butt joints inside each plank row, in place, so a row reads as one long board.

    A joint is a narrow dark line of the row's occlusion across its width (the height map steps
    between boards rather than dipping). Its columns are refilled by
    cross-fading the texture mirrored from both sides, which matches the neighbors at the window's
    edges (the normal map's x is negated where mirrored). Returns how many joints were closed.
    """
    from scipy.ndimage import gaussian_filter1d
    from scipy.signal import find_peaks

    width = maps["disp"].shape[1]
    closed = 0
    for top, bottom in rows:
        inner = slice(top + (bottom - top) // 5, bottom - (bottom - top) // 5)
        profile = gaussian_filter1d(maps["ao"][inner].mean(axis=0), 1.0, mode="wrap")
        depth = np.median(profile) - profile
        spread = np.median(np.abs(profile - np.median(profile))) + 1e-6
        dips, props = find_peaks(np.r_[depth, depth[:32]], prominence=8 * spread, width=(1, width // 80))
        keep = dips < width
        dips, props = dips[keep], {key: value[keep] for key, value in props.items()}
        for dip, half in zip(dips % width, props["widths"] / 2):
            w = int(np.ceil(half)) + 3
            columns = np.arange(dip - w, dip + w + 1)
            t = (columns - (dip - w)) / (2 * w)
            left, right = (2 * (dip - w) - columns) % width, (2 * (dip + w) - columns) % width
            for name, image in maps.items():
                band = image[top:bottom]
                a, b = band[:, left].copy(), band[:, right].copy()
                if name == "nor_gl":
                    a[..., 0], b[..., 0] = 1 - a[..., 0], 1 - b[..., 0]
                weight = t[None, :, None] if band.ndim == 3 else t[None, :]
                band[:, columns % width] = a * (1 - weight) + b * weight
            closed += 1
    return closed


def even_tone(color: np.ndarray, rows: list[tuple[int, int]], strength: float, meters_per_px: float) -> None:
    """Pull each board's broad color toward the scan's mean, keeping the grain, in place."""
    from scipy.ndimage import gaussian_filter1d

    mean = color.reshape(-1, 3).mean(axis=0)
    sigma = 0.06 / meters_per_px
    for top, bottom in rows:
        band = color[top:bottom]
        broad = gaussian_filter1d(band.mean(axis=0), sigma, axis=0, mode="wrap")
        band *= ((mean / np.maximum(broad, 1e-3)) ** strength)[None]
        np.clip(band, 0.0, 1.0, out=band)


def rotate_normals(normal: np.ndarray) -> np.ndarray:
    """Turn a tangent-space normal map image a quarter turn counter-clockwise, vectors included."""
    turned = np.rot90(normal, k=1).copy()
    x, y = turned[..., 0] * 2 - 1, turned[..., 1] * 2 - 1
    turned[..., 0], turned[..., 1] = (-y + 1) / 2, (x + 1) / 2
    return turned


def match_color(source: np.ndarray, target: np.ndarray, strength: float = 1.0, gain: float = 1.0) -> np.ndarray:
    """Shift ``source`` toward ``target``'s per-channel mean and spread, keeping its own detail."""
    s_mean, s_std = source.reshape(-1, 3).mean(0), source.reshape(-1, 3).std(0) + 1e-6
    t_mean, t_std = target.reshape(-1, 3).mean(0), target.reshape(-1, 3).std(0)
    graded = (source - s_mean) / s_std * t_std + t_mean
    return np.clip((source + (graded - source) * strength) * gain, 0.0, 1.0)


def tile(maps: dict[str, np.ndarray], across: int, down: int) -> dict[str, np.ndarray]:
    return {name: np.tile(image, (down, across) + ((1,) if image.ndim == 3 else ())) for name, image in maps.items()}


def paint_shingles(maps: dict[str, np.ndarray], meters_per_px: float, rng: np.random.Generator) -> list[tuple[int, int]]:
    """Cut the region into bands of shingles: joints, a tone per shingle, a darker butt. In place."""
    height, width = maps["diff"].shape[:2]
    bands = [(round(i * height / SHINGLE_BANDS), round((i + 1) * height / SHINGLE_BANDS)) for i in range(SHINGLE_BANDS)]
    joint = max(2, round(SHINGLE_JOINT / meters_per_px))
    edge = max(2, joint // 2 + 1)
    for top, bottom in bands:
        # Joints repeat with the atlas width, so the band tiles along U like every other region.
        cuts, x = [], 0.0
        while True:
            x += rng.uniform(*SHINGLE_WIDTH) / meters_per_px
            if x > width - SHINGLE_WIDTH[0] / meters_per_px:
                break
            cuts.append(int(x))
        cuts = [0] + cuts
        tones = rng.uniform(0.8, 1.12, len(cuts))
        tone = np.empty(width, dtype=np.float32)
        for i, start in enumerate(cuts):
            end = cuts[i + 1] if i + 1 < len(cuts) else width
            tone[start:end] = tones[i]
        gap = np.zeros(width, dtype=bool)
        slope = np.zeros(width, dtype=np.float32)
        for cut in cuts:
            columns = np.arange(cut - joint // 2, cut + joint - joint // 2) % width
            gap[columns] = True
            slope[np.arange(cut - joint // 2 - edge, cut - joint // 2) % width] = 0.55
            slope[np.arange(cut + joint - joint // 2, cut + joint - joint // 2 + edge) % width] = -0.55
        rows = slice(top, bottom)
        # The lower (butt) edge of each band sits in the shadow of the row below's overlap.
        butt = (1.0 - 0.28 * np.linspace(0.0, 1.0, bottom - top, dtype=np.float32) ** 4)[:, None]
        maps["diff"][rows] *= (tone * np.where(gap, 0.35, 1.0))[None, :, None] * butt[..., None]
        maps["ao"][rows] *= np.where(gap, 0.3, 1.0)[None, :] * butt
        maps["rough"][rows] = np.clip(maps["rough"][rows] + np.where(gap, 0.12, 0.0)[None, :], 0, 1)
        normal = maps["nor_gl"][rows]
        nx = normal[..., 0] * 2 - 1 + slope[None, :]
        normal[..., 0] = np.clip((nx + 1) / 2, 0, 1)
    return bands


def compose(materials: dict[str, Path], width: int, seed: int) -> tuple[dict[str, np.ndarray], dict]:
    rng = np.random.default_rng(seed)
    planks = load_material(materials["planks"], width)
    period = float(json.loads((materials["planks"] / "material.json").read_text("utf-8"))["sizeMeters"])
    rows = plank_rows(planks["disp"])
    joints = remove_joints(planks, rows)
    even_tone(planks["diff"], rows, 0.85, period / width)
    wood_size = float(json.loads((materials["timber"] / "material.json").read_text("utf-8"))["sizeMeters"])
    # A whole number of rough-wood tiles across the atlas, so the region wraps with the planks.
    across = min((d for d in range(1, 65) if width % d == 0), key=lambda d: abs(d - period / wood_size))
    tile_px = width // across
    down = -(-(width // 2) // tile_px)
    wood = load_material(materials["timber"], tile_px)

    timber = dict(wood)
    timber["diff"] = match_color(wood["diff"], planks["diff"], strength=0.85, gain=0.93)
    timber = {name: np.rot90(image, k=1).copy() for name, image in timber.items()}
    timber["nor_gl"] = rotate_normals(wood["nor_gl"])
    timber = {name: image[: width // 2] for name, image in tile(timber, across, down).items()}

    shingles = dict(wood)
    shingles["diff"] = match_color(wood["diff"], planks["diff"], strength=0.6, gain=0.72)
    shingles = {name: image[: width // 2].copy() for name, image in tile(shingles, across, down).items()}
    shingle_bands = paint_shingles(shingles, period / width, rng)

    atlas = {name: np.concatenate([planks[name], timber[name], shingles[name]], axis=0)
             for name in ("diff", "nor_gl", "rough", "ao")}
    height = atlas["diff"].shape[0]

    def v(pixel_top: int, pixel_bottom: int) -> list[float]:
        # UV V runs up from the bottom of the image.
        return [round(1 - pixel_bottom / height, 6), round(1 - pixel_top / height, 6)]

    layout = {
        "periodMeters": period, "width": width, "height": height, "seed": seed, "jointsClosed": joints,
        "regions": {
            "planks": {"v": v(0, width), "metersTall": period, "rows": [v(top, bottom) for top, bottom in rows],
                       "rowMeters": [round((bottom - top) / width * period, 4) for top, bottom in rows]},
            "timber": {"v": v(width, width + width // 2), "metersTall": period / 2},
            "shingles": {"v": v(width + width // 2, height), "metersTall": period / 2,
                         "bands": [v(width + width // 2 + top, width + width // 2 + bottom) for top, bottom in shingle_bands]},
        },
    }
    return atlas, layout


def save(atlas: dict[str, np.ndarray], layout: dict, output: Path, name: str) -> dict[str, str]:
    from PIL import Image

    output.mkdir(parents=True, exist_ok=True)

    def write(key: str, array: np.ndarray) -> str:
        path = output / f"T_{name}_{key}.png"
        Image.fromarray(np.clip(np.round(array * 255), 0, 255).astype(np.uint8)).save(path, optimize=False)
        return path.name

    files = {"basecolor": write("basecolor", atlas["diff"]), "normal": write("normal", atlas["nor_gl"]),
             "roughness": write("roughness", atlas["rough"]), "occlusion": write("occlusion", atlas["ao"])}
    packed = np.zeros(atlas["diff"].shape[:2] + (4,), dtype=np.float32)
    packed[..., 1] = atlas["ao"]
    packed[..., 3] = 1.0 - atlas["rough"]
    files["unityMetallicSmoothness"] = write("metallicsmoothness", packed)
    preview = Image.open(output / files["basecolor"])
    preview.thumbnail((512, 1024))
    preview.save(output / "atlas-preview.png")
    files["preview"] = "atlas-preview.png"
    (output / "layout.json").write_text(json.dumps({**layout, "files": files}, indent=2), "utf-8")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planks", required=True, type=Path, help="horizontal plank scan folder (diff, nor_gl, rough, ao, disp)")
    parser.add_argument("--timber", required=True, type=Path, help="long-grain rough wood scan folder")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--width", type=int, default=2048)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    print(progress_line(0, 2, "재질 불러와 트림 시트 만드는 중"), flush=True)
    atlas, layout = compose({"planks": args.planks, "timber": args.timber}, args.width, args.seed)
    print(progress_line(1, 2, "맵 저장 중"), flush=True)
    save(atlas, layout, args.output, args.name)
    print(progress_line(2, 2, f"판자 줄 {len(layout['regions']['planks']['rows'])}개 · {args.width}×{layout['height']}"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
