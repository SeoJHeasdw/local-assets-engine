"""Normalize a generated mesh for game engines with Blender (bpy).

Joins parts, applies transforms, decimates to a face budget, puts the pivot at
the bottom center, scales the longest side to ``--size`` meters, exports GLB.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import progress_line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stats", required=True, type=Path)
    parser.add_argument("--target-faces", type=int, default=0, help="0 keeps every face")
    parser.add_argument("--size", type=float, default=1.0, help="longest side in meters, 0 keeps scale")
    args = parser.parse_args(argv)

    import bpy
    import numpy as np
    from mathutils import Matrix, Vector

    print(progress_line(0, 4, "메시 불러오는 중"), flush=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(args.input))
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not meshes:
        raise SystemExit("입력 GLB에 메시가 없습니다.")

    for obj in meshes:
        world = obj.matrix_world.copy()
        obj.parent = None
        obj.matrix_world = world
    for obj in [o for o in bpy.context.scene.objects if o.type != "MESH"]:
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    faces_in = len(obj.data.polygons)
    print(progress_line(1, 4, f"면 {faces_in:,}개 정리 중"), flush=True)
    if args.target_faces and faces_in > args.target_faces:
        modifier = obj.modifiers.new("decimate", "DECIMATE")
        modifier.decimate_type = "COLLAPSE"
        modifier.ratio = args.target_faces / faces_in
        bpy.ops.object.modifier_apply(modifier=modifier.name)

    mesh = obj.data
    coords = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", coords)
    coords = coords.reshape(-1, 3)
    low, high = coords.min(axis=0), coords.max(axis=0)
    dimensions = high - low
    longest = float(dimensions.max())
    scale = args.size / longest if args.size and longest > 0 else 1.0
    offset = Vector((-(low[0] + high[0]) / 2, -(low[1] + high[1]) / 2, -low[2]))
    mesh.transform(Matrix.Scale(scale, 4) @ Matrix.Translation(offset))
    # 생성된 메시는 중복 면·잘못된 참조를 품고 있어 내보내기가 "유효하지 않다"고 경고한다.
    repaired = mesh.validate(verbose=False)
    mesh.update()
    obj.name = args.output.stem

    print(progress_line(2, 4, "GLB 내보내는 중"), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(filepath=str(args.output), export_format="GLB", export_yup=True)

    images = {
        node.image.name
        for material in mesh.materials if material and material.node_tree
        for node in material.node_tree.nodes if node.type == "TEX_IMAGE" and node.image
    }
    stats = {
        "repaired": bool(repaired),
        "facesIn": faces_in,
        "facesOut": len(mesh.polygons),
        "triangles": int(sum(len(poly.vertices) - 2 for poly in mesh.polygons)),
        "vertices": len(mesh.vertices),
        # Blender는 Z가 위, glTF는 Y가 위다. 여기 값은 [가로, 깊이, 높이] 미터다.
        "dimensionsMeters": [round(float(v) * scale, 4) for v in dimensions],
        "scale": round(scale, 6),
        "materials": [material.name for material in mesh.materials if material],
        "textures": len(images),
        "bytes": args.output.stat().st_size,
    }
    args.stats.write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")
    print(progress_line(4, 4, f"면 {stats['facesOut']:,}개"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
