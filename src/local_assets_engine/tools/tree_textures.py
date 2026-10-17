"""Textures for one tree from CC0 scans, before Blender builds it (``tree_build``).

Bark: the whole scan becomes one tile that repeats around and along the trunk at its real size (see
``bark_maps``), packed with the tree mask convention (R metallic 0, G occlusion, B leaf translucency = 0,
A smoothness).

Leaves: a leaf or twig texture cut from a tree scan holds loose leaves (or sprigs) on a padded atlas. Each
connected piece of its alpha becomes an element with a base (where it hangs from the twig), a tip and a width,
so Blender can lay real leaves along twigs. Pieces that touch the edge (bark strips, cut branches), specks and
pieces far larger than the typical one are left out. The color is graded toward yellow-olive here once.

Rings: the cut face of the stump is the CC0 ``rough_wood`` scan wrapped into polar coordinates (its grain lines
become rings), toned to fresh wood with a dark pith and the bark's color at the rim.

Outputs in ``--output``: ``bark/T_tree_<name>_bark_{basecolor,normal,mask}.png`` and ``bark.json``,
``leaves/{diff,alpha,nor,rough,ao}.png`` with ``elements.json`` and ``elements.png``, ``ring/ring_{basecolor,
normal,mask}.png``, and ``textures.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from . import progress_line
from .terrain_tile import (_read, directional_light, flatten, grade, linear_to_srgb, load_scan, luminance, neon_fraction,
                           preview, remove_directional, rgb_to_hsv, srgb_to_linear, wrap_seam_ratio)

# 잎 조각: 이보다 작은 점(픽셀 수, 원본 크기 대비)은 쓰지 않는다.
MIN_ELEMENT_FRACTION = 0.0004
EDGE_TOUCH_FRACTION = 0.06
# 길이가 폭의 이 배를 넘는 조각은 잎 없는 줄기 토막이다.
MAX_ASPECT = 6.0
# 잎으로 보는 평균 색상 범위(도). 노랗게 든 잎(약 50°)은 넣고 갈색 가지(약 25°)는 뺀다.
LEAF_HUE = (45.0, 170.0)
BASE_HINTS = {"down": (0.0, 1.0), "up": (0.0, -1.0), "right": (1.0, 0.0), "left": (-1.0, 0.0)}
RING_PX = 512


def byte(values: np.ndarray) -> np.ndarray:
    return (np.clip(values, 0, 1) * 255 + 0.5).astype(np.uint8)


def save(array: np.ndarray, path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(array).save(path)


def bark_maps(spec: dict, report) -> tuple[dict[str, np.ndarray], dict]:
    """The whole bark scan as one ``size``² tile of its real size (1.0 × 2.0 m bark gives 1024 px/m across and
    512 px/m along the trunk), with the baked sun divided out, large blotches evened and the color graded.

    The bark scans step at their own wrap by 0.66–1.26 times an ordinary neighbor step, so they repeat as they
    are. Quilting them was worse: their furrows run the length of the trunk, and every horizontal seam between
    square patches cut the furrows off in a visible line.
    """
    size = int(spec["size"])
    source = spec["scan"]
    width, height = source.get("dimensionsMeters") or (source["sizeMeters"], source["sizeMeters"])
    report(0.05, "껍질 스캔 불러오는 중")
    maps = load_scan(Path(source["dir"]), size)
    slope = directional_light(maps["color"], maps["normal"])
    left = remove_directional(maps, slope)
    stats: dict = {"id": source["id"], "tileMeters": [float(width), float(height)],
                   "pxPerMeter": [round(size / float(width), 1), round(size / float(height), 1)],
                   "directionalLight": [round(v, 4) for v in slope], "directionalLightAfter": [round(v, 4) for v in left]}
    # 얼룩 크기는 짧은 변(둘레 방향) 픽셀로 잰다.
    stats["flatten"] = flatten(maps, float(spec["flatten"]), float(spec["flattenMeters"]) * size / float(width))
    graded = spec.get("grade") or {}
    if graded:
        maps["color"] = grade(maps["color"], graded.get("hueShift", 0.0), graded.get("saturation", 1.0),
                              graded.get("value", 1.0), graded.get("maxSaturation"))
    stats["grade"] = graded
    stats["meanBaseColorSrgb"] = [round(float(v), 4) for v in linear_to_srgb(maps["color"]).reshape(-1, 3).mean(axis=0)]
    report(0.5, "껍질 맵 묶는 중")
    return maps, stats


def bark(spec: dict, output: Path, report) -> dict:
    """The bark tile, packed with B = 0 (bark lets no light through)."""
    maps, stats = bark_maps(spec, report)
    mask = np.stack([np.zeros_like(maps["rough"]), maps["ao"], np.zeros_like(maps["rough"]), 1 - maps["rough"]], axis=-1)
    images = {"basecolor": byte(linear_to_srgb(maps["color"])), "normal": byte(maps["normal"] * 0.5 + 0.5), "mask": byte(mask)}
    name = spec["name"]
    files = {}
    for key, array in images.items():
        files[key] = f"T_tree_{name}_bark_{key}.png"
        save(array, output / files[key])
    meters = "×".join(f"{v:g}" for v in stats["tileMeters"])
    board = preview(images, f"T_tree_{name}_bark · {meters} m · {spec['size']} px", blue="B translucency")
    board.save(output / "bark_preview.png")
    files["preview"] = "bark_preview.png"
    stats.update({"size": spec["size"], "files": files,
                  "wrapSeamRatio": round(max(wrap_seam_ratio(luminance(maps["color"])),
                                             wrap_seam_ratio(maps["normal"][..., 0])), 3)})
    (output / "bark.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")
    return stats


def elements(alpha: np.ndarray, color: np.ndarray, base_hint: str, open_px: int = 1, axis_mode: str = "pca",
             area_range: tuple[float, float] = (0.25, 4.0)) -> tuple[list[dict], np.ndarray]:
    """Loose leaves (or sprigs) in a leaf atlas: base, tip and width of each connected alpha piece, in pixels,
    and the alpha cut down to those pieces.

    ``open_px`` opens the mask before labeling: some scans' alpha also covers the stretched padding between
    leaves (``tree_small_02``), streaks that join every leaf into one piece. Opening restores the blades but
    not the streaks; the kept pieces then grow back only 2 px inside the original alpha for the soft edge.
    Pieces whose mean color is not green to yellow (``LEAF_HUE``: bare twigs, a bark strip) are not leaves, and
    pieces outside ``area_range`` times the median piece are slivers of padding or leaves grown together.
    The long axis is the piece's principal axis (``pca``) or the hint itself (``hint``, for forked sprigs whose
    principal axis runs diagonally); which end hangs from the twig comes from ``base_hint`` (scans lay their
    leaves the same way round: island leaves stalk down, small-tree leaves stalk right).
    """
    from scipy.ndimage import binary_dilation, binary_opening, find_objects, label

    raw = alpha > 0.5
    structure = np.ones((3, 3), dtype=bool)
    solid = binary_opening(raw, structure, iterations=max(1, open_px))
    labels, count = label(solid)
    size = alpha.shape[0]
    areas = np.bincount(labels.ravel())[1:]
    hint = np.array(BASE_HINTS[base_hint])
    found = []
    for index, box in enumerate(find_objects(labels), start=1):
        area = int(areas[index - 1])
        if area < MIN_ELEMENT_FRACTION * size * size:
            continue
        ys, xs = box
        py, px = np.nonzero(labels[box] == index)
        # 가장자리를 따라 길게 닿은 조각(껍질 띠, 잘린 가지)은 뺀다. 잎자루 끝만 닿은 잎은 쓴다.
        edge = int(np.sum((py + ys.start <= 1) | (px + xs.start <= 1) | (py + ys.start >= size - 2) | (px + xs.start >= size - 2)))
        if edge > EDGE_TOUCH_FRACTION * size:
            continue
        points = np.stack([px + xs.start, py + ys.start], axis=1).astype(np.float64)
        center = points.mean(axis=0)
        if axis_mode == "hint":
            axis = -hint.astype(np.float64)
        else:
            _, _, vt = np.linalg.svd(points - center, full_matrices=False)
            axis = vt[0] if vt[0] @ hint < 0 else -vt[0]  # from the base toward the tip
        along = (points - center) @ axis
        across = (points - center) @ np.array([-axis[1], axis[0]])
        length, width = float(along.max() - along.min()), float(across.max() - across.min())
        if length > MAX_ASPECT * width:
            continue  # 잎 없는 줄기 토막
        hue, saturation = _hue(color[py + ys.start, px + xs.start].mean(axis=0))
        if not (LEAF_HUE[0] <= hue <= LEAF_HUE[1]) or saturation < 0.12:
            continue  # 갈색 맨가지·껍질
        found.append({"label": index, "area": area, "box": [int(xs.start), int(ys.start), int(xs.stop), int(ys.stop)],
                      "base": [round(float(v), 2) for v in center + axis * along.min()],
                      "tip": [round(float(v), 2) for v in center + axis * along.max()],
                      "lengthPx": round(length, 2), "widthPx": round(width, 2),
                      "offsetPx": round(float((across.max() + across.min()) / 2), 2)})
    if not found:
        return [], np.zeros_like(alpha)
    median = float(np.median([e["area"] for e in found]))
    kept = [e for e in found if median * area_range[0] <= e["area"] <= median * area_range[1]]
    keep = np.isin(labels, [e.pop("label") for e in kept])
    for e in found:
        e.pop("label", None)
    keep = binary_dilation(keep, structure, iterations=2) & raw
    cleaned = np.where(keep, alpha, 0.0).astype(np.float32)
    return sorted(kept, key=lambda e: (e["box"][1], e["box"][0])), cleaned


def _hue(linear_rgb: np.ndarray) -> tuple[float, float]:
    hsv = rgb_to_hsv(linear_to_srgb(np.asarray(linear_rgb, dtype=np.float32))[None])[0]
    return float(hsv[0]), float(hsv[1])


def leaves(spec: dict, output: Path, report) -> dict:
    """Graded leaf maps for Blender and the elements found in them."""
    folder = Path(spec["dir"])
    report(0.6, "잎 원본 읽는 중")
    color = srgb_to_linear(_read(folder / "diff.jpg", "RGB"))
    alpha = _read(folder / "alpha.jpg", "L")
    found, alpha = elements(alpha, color, spec["base"], int(spec.get("openPx", 1)), spec.get("axis", "pca"),
                            tuple(spec.get("areaRange", (0.25, 4.0))))
    solid = alpha > 0.5
    before = neon_fraction(color[solid])
    graded = spec.get("grade") or {}
    if graded:
        color = grade(color, graded.get("hueShift", 0.0), graded.get("saturation", 1.0), graded.get("value", 1.0),
                      graded.get("maxSaturation"))
    out = output / "leaves"
    save(byte(linear_to_srgb(color)), out / "diff.png")
    save(byte(alpha), out / "alpha.png")
    for key, source, mode in (("nor", "nor_gl.jpg", "RGB"), ("rough", "rough.jpg", "L"), ("ao", "ao.jpg", "L")):
        save(byte(_read(folder / source, mode)), out / f"{key}.png")
    debug(color, alpha, found, out / "elements.png")
    stats = {"elements": len(found), "neonBefore": round(before, 5), "neonFraction": round(neon_fraction(color[solid]), 5),
             "meanColorSrgb": [round(float(v), 4) for v in linear_to_srgb(color[solid]).mean(axis=0)],
             "size": int(alpha.shape[0]), "grade": graded}
    (out / "elements.json").write_text(json.dumps({"size": int(alpha.shape[0]), "elements": found}, indent=1), "utf-8")
    return stats


def debug(color: np.ndarray, alpha: np.ndarray, found: list[dict], path: Path) -> None:
    """The scan with each element's base (red) to tip (yellow), to check the base hint by eye."""
    from PIL import Image, ImageDraw

    shown = linear_to_srgb(color) * alpha[..., None] + 0.75 * (1 - alpha[..., None])
    image = Image.fromarray(byte(shown))
    draw = ImageDraw.Draw(image)
    width = max(2, image.width // 400)
    for element in found:
        draw.line([tuple(element["base"]), tuple(element["tip"])], fill=(255, 220, 0), width=width)
        x, y = element["base"]
        r = width * 4
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(230, 30, 30))
    image.thumbnail((1024, 1024))
    image.save(path)


def rings(spec: dict, bark_color: np.ndarray, output: Path, seed: int) -> dict:
    """The stump's cut face: wood grain wrapped into rings, a dark pith, sapwood, the bark at the rim.

    ``rough_wood``'s grain runs down the image, so the column across the grain becomes the radius and the
    row along the grain becomes the angle.
    """
    from scipy.ndimage import gaussian_filter, map_coordinates

    folder = Path(spec["dir"])
    wood = srgb_to_linear(_read(folder / "diff.jpg", "RGB"))
    rough = _read(folder / "rough.jpg", "L")
    rng = np.random.default_rng(seed)
    size = RING_PX
    yy, xx = (np.mgrid[0:size, 0:size] + 0.5) / size * 2 - 1
    radius = np.hypot(xx, yy)
    angle = (np.arctan2(yy, xx) / (2 * math.pi)) % 1.0
    height, width = wood.shape[:2]
    # 반지름 방향으로 원본의 가로 1/3을, 둘레로 세로 전체를 쓴다(결 한 줄이 나이테 한 줄).
    offset = rng.uniform(0, width * 0.6)
    wobble = gaussian_filter(rng.standard_normal(64), 3, mode="wrap")
    wobble = np.interp(angle * 64, np.arange(65), np.append(wobble, wobble[0])) * 0.02
    column = offset + (radius + wobble) * width / 3
    row = angle * height
    coordinates = np.stack([row.ravel(), column.ravel()])
    detail = np.stack([map_coordinates(wood[..., c], coordinates, order=1, mode="grid-wrap").reshape(size, size)
                       for c in range(3)], axis=-1)
    lum = luminance(detail)
    contrast = (lum / max(float(lum.mean()), 1e-4)) ** 0.8
    heart = np.array(spec["heartColor"], dtype=np.float32)
    sap = np.array(spec["sapColor"], dtype=np.float32)
    mix = np.clip((radius - 0.45) / 0.4, 0, 1)[..., None]
    wood_color = srgb_to_linear((heart * (1 - mix) + sap * mix)) * contrast[..., None]
    pith = np.exp(-(radius / 0.04) ** 2)[..., None]
    wood_color = wood_color * (1 - 0.55 * pith)
    rim = np.clip((radius - 0.93) / 0.025, 0, 1)[..., None]
    bark_tone = srgb_to_linear(bark_color.astype(np.float32)) * (0.8 + 0.4 * rng.random((size, size, 1)).astype(np.float32))
    color = wood_color * (1 - rim) + bark_tone * rim
    inside = (radius <= 1.0).astype(np.float32)
    # 노멀: 결 밝기를 아주 얕은 높이로 쓴다(톱질한 면은 거의 평평하다).
    bump = gaussian_filter(lum, 1.0) * 0.6
    gy, gx = np.gradient(bump)
    normal = np.stack([-gx, gy, np.ones_like(gx)], axis=-1)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    smooth = 1 - np.clip(map_coordinates(rough, coordinates, order=1, mode="grid-wrap").reshape(size, size), 0.55, 1)
    mask = np.stack([np.zeros_like(lum), np.clip(0.75 + 0.25 * contrast, 0, 1), np.zeros_like(lum), smooth], axis=-1)
    out = output / "ring"
    save(np.concatenate([byte(linear_to_srgb(color)), byte(inside)[..., None]], axis=-1), out / "ring_basecolor.png")
    save(byte(normal * 0.5 + 0.5), out / "ring_normal.png")
    save(byte(mask), out / "ring_mask.png")
    return {"source": spec["id"], "px": size}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text("utf-8"))
    started = time.monotonic()

    def report(fraction: float, detail: str) -> None:
        print(progress_line(round(fraction * 1000), 1000, detail), flush=True)

    args.output.mkdir(parents=True, exist_ok=True)
    bark_spec = {**spec["bark"], "name": spec["name"], "seed": spec["seed"]}
    bark_stats = bark(bark_spec, args.output / "bark", report)
    leaf_stats = leaves(spec["leaves"], args.output, report)
    report(0.9, "나이테 만드는 중")
    from PIL import Image

    bark_color = np.asarray(Image.open(args.output / "bark" / bark_stats["files"]["basecolor"]).convert("RGB"),
                            dtype=np.float32).reshape(-1, 3).mean(axis=0) / 255.0
    ring_stats = rings(spec["rings"], bark_color, args.output, spec["seed"])
    summary = {"bark": bark_stats, "leaves": leaf_stats, "rings": ring_stats,
               "seconds": round(time.monotonic() - started, 2)}
    (args.output / "textures.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    report(1.0, f"잎 조각 {leaf_stats['elements']}개 · 껍질 이음매 비 {bark_stats['wrapSeamRatio']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
