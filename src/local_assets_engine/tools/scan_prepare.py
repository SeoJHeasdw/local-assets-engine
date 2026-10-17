"""A scanned CC0 model as a game-asset master: aligned, its bottom cut flat, sized and color graded (Blender ``bpy``).

1. Import the glTF and join its meshes (Blender, Z up).
2. Turn about Z so the longest horizontal axis of the points lies along X, then by ``--yaw``.
3. Cut away everything below a horizontal plane and close the cut with a flat cap: the part that sits in
   the ground. The cut removes at least ``--bury`` of the height. With ``--width``, it is lowered or raised
   (up to ``MAX_CUT`` of the height) so height to width after the cut comes closest to the request; what
   is left is made up by stretching X and Y (``stretchXY``, 1 = none).
4. Scale to ``--height`` with the bottom center at the origin.
5. Grade the base color texture in HSV: saturation and value (e.g. an orange-stained rock to grey-brown).
6. Write the master GLB (glTF, Y up) with its textures and ``--stats``.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from . import progress_line

MAX_CUT = 0.7


def _align(obj, yaw_degrees: float) -> float:
    """Turn ``obj`` about Z so its longest horizontal spread runs along X; returns the angle in degrees."""
    import numpy as np
    from mathutils import Matrix

    from .game_bake import mesh_arrays

    coords = mesh_arrays(obj)[0]
    xy = coords[:: max(1, len(coords) // 200_000), :2]
    _, _, axes = np.linalg.svd(xy - xy.mean(axis=0), full_matrices=False)
    angle = -math.atan2(axes[0][1], axes[0][0]) + math.radians(yaw_degrees)
    obj.data.transform(Matrix.Rotation(angle, 4, "Z"))
    obj.data.update()
    return math.degrees(angle)


def choose_cut(coords, bury: float, target_ratio: float | None) -> tuple[float, float]:
    """Cut height (fraction of the height from the bottom) and the height:width the points above it keep."""
    import numpy as np

    low, high = float(coords[:, 2].min()), float(coords[:, 2].max())
    span = high - low

    def ratio(fraction):
        above = coords[coords[:, 2] >= low + fraction * span]
        width = float(np.ptp(above[:, 0])) if len(above) > 2 else 1e-6
        return (1 - fraction) * span / max(width, 1e-6)

    if target_ratio is None:
        return bury, ratio(bury)
    fractions = np.linspace(bury, max(bury, MAX_CUT), 60)
    best = min(fractions, key=lambda f: abs(math.log(ratio(f) / target_ratio)))
    return float(best), ratio(best)


def _cut(obj, height: float) -> int:
    """Remove the mesh below ``z = height`` and cap the opening; returns the cap's face count."""
    import bmesh

    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    # glTF splits vertices along UV seams, so every seam would look like a hole to fill. Weld them first:
    # UVs are stored per face corner and stay as they were.
    bmesh.ops.remove_doubles(mesh, verts=mesh.verts[:], dist=1e-6)
    geometry = mesh.verts[:] + mesh.edges[:] + mesh.faces[:]
    bmesh.ops.bisect_plane(mesh, geom=geometry, dist=1e-7, plane_co=(0, 0, height), plane_no=(0, 0, 1),
                           clear_inner=True)
    boundary = [edge for edge in mesh.edges if edge.is_boundary]
    before = set(mesh.faces)
    bmesh.ops.holes_fill(mesh, edges=boundary, sides=0)
    cap = [face for face in mesh.faces if face not in before]
    bmesh.ops.triangulate(mesh, faces=cap)
    cap = {face for face in mesh.faces if face not in before}
    uv = mesh.loops.layers.uv.active
    for face in cap:
        face.normal_update()
        if face.normal.z > 0:
            face.normal_flip()
        if uv is None:
            continue
        # The buried cap takes the rock texture of the side face next to each corner.
        for loop in face.loops:
            side = next((other for other in loop.vert.link_loops if other.face not in cap), None)
            if side is not None:
                loop[uv].uv = side[uv].uv
    mesh.to_mesh(obj.data)
    mesh.free()
    obj.data.update()
    return len(cap)


def _grade_base_color(obj, saturation: float, value: float) -> list[str]:
    """Scale the HSV saturation and value of every base color texture, in place. Returns the image names."""
    import numpy as np

    from .terrain_tile import hsv_to_rgb, rgb_to_hsv

    graded = []
    for slot in obj.material_slots:
        material = slot.material
        if not material or not material.node_tree:
            continue
        for node in material.node_tree.nodes:
            if node.type != "BSDF_PRINCIPLED" or not node.inputs["Base Color"].is_linked:
                continue
            source = node.inputs["Base Color"].links[0].from_node
            image = getattr(source, "image", None)
            if image is None or image.name in graded:
                continue
            width, height = image.size
            pixels = np.empty(width * height * 4, dtype=np.float32)
            image.pixels.foreach_get(pixels)
            pixels = pixels.reshape(height, width, 4)
            hsv = rgb_to_hsv(pixels[..., :3])
            hsv[..., 1] *= saturation
            hsv[..., 2] *= value
            pixels[..., :3] = np.clip(hsv_to_rgb(np.clip(hsv, 0, None)), 0, 1)
            image.pixels.foreach_set(pixels.ravel())
            image.update()
            graded.append(image.name)
    return graded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gltf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--height", type=float, required=True)
    parser.add_argument("--width", type=float)
    parser.add_argument("--bury", type=float, default=0.05)
    parser.add_argument("--yaw", type=float, default=0.0)
    parser.add_argument("--saturation", type=float, default=1.0)
    parser.add_argument("--value", type=float, default=1.0)
    args = parser.parse_args(argv)

    import bpy
    import numpy as np

    from .blender_post import topology_checks
    from .game_bake import _load_joined, mesh_arrays

    print(progress_line(0, 5, "스캔 불러오는 중"), flush=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    obj = _load_joined(args.gltf, "scan")
    source = mesh_arrays(obj)[0]
    source_dimensions = [round(float(v), 4) for v in np.ptp(source, axis=0)]
    angle = _align(obj, args.yaw)

    print(progress_line(1, 5, "땅에 묻힐 바닥 자르는 중"), flush=True)
    coords = mesh_arrays(obj)[0]
    target = args.height / args.width if args.width else None
    fraction, kept_ratio = choose_cut(coords, args.bury, target)
    low, high = float(coords[:, 2].min()), float(coords[:, 2].max())
    cap_faces = _cut(obj, low + fraction * (high - low))

    print(progress_line(2, 5, "크기 맞추는 중"), flush=True)
    coords = mesh_arrays(obj)[0]
    lower, upper = coords.min(axis=0), coords.max(axis=0)
    scale = args.height / float(upper[2] - lower[2])
    stretch = args.width / (float(upper[0] - lower[0]) * scale) if args.width else 1.0
    from mathutils import Matrix

    center = Matrix.Translation((-(lower[0] + upper[0]) / 2, -(lower[1] + upper[1]) / 2, -lower[2]))
    obj.data.transform(Matrix.Diagonal((scale * stretch, scale * stretch, scale, 1.0)) @ center)
    obj.data.update()

    print(progress_line(3, 5, "색 보정 중"), flush=True)
    graded = _grade_base_color(obj, args.saturation, args.value) if (args.saturation, args.value) != (1.0, 1.0) else []

    print(progress_line(4, 5, "품질본 저장 중"), flush=True)
    coords, triangles = mesh_arrays(obj)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.export_scene.gltf(filepath=str(args.output), export_format="GLB", use_selection=True, export_yup=True,
                              export_image_format="JPEG", export_jpeg_quality=92)
    dimensions = [round(float(v), 4) for v in np.ptp(coords, axis=0)]
    stats = {"sourceDimensions": source_dimensions, "alignDegrees": round(angle, 2), "cutFraction": round(fraction, 4),
             "heightToWidth": round(kept_ratio, 4), "stretchXY": round(stretch, 4), "capFaces": cap_faces,
             "dimensions": dimensions, "sizeMeters": round(max(dimensions), 4), "triangles": len(triangles),
             "vertices": len(coords), "grade": {"saturation": args.saturation, "value": args.value, "images": graded},
             "topology": topology_checks(coords, triangles)}
    args.stats.write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")
    print(progress_line(5, 5, f"{dimensions[0]:.2f} × {dimensions[1]:.2f} × {dimensions[2]:.2f} m · 삼각형 {len(triangles):,}"),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
