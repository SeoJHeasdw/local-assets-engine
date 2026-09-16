"""Normalize a generated mesh for game engines with Blender (bpy).

Joins parts, applies transforms, decimates to a face budget, puts the pivot at
the bottom center, scales the longest side to ``--size`` meters, exports GLB.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import progress_line


def topology_checks(vertices, triangles):
    """Inspect geometric adjacency across UV seams, without changing the mesh."""
    import numpy as np

    # Exporters split vertices at UV/normal seams. Exact coincident positions
    # should count as one vertex for inspection, but must retain their UVs.
    _, inverse = np.unique(vertices, axis=0, return_inverse=True)
    faces = inverse[triangles]
    edges = np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    canonical = np.sort(edges, axis=1)
    _, edge_ids, counts = np.unique(canonical, axis=0, return_inverse=True, return_counts=True)
    direction = np.where(edges[:, 0] < edges[:, 1], 1, -1)
    balance = np.bincount(edge_ids, weights=direction, minlength=len(counts))
    checks = {
        "boundaryEdges": int(np.count_nonzero(counts == 1)),
        "nonManifoldEdges": int(np.count_nonzero(counts > 2)),
        "inconsistentWindingEdges": int(np.count_nonzero((counts == 2) & (balance != 0))),
        "duplicateFaces": len(faces) - len(np.unique(np.sort(faces, axis=1), axis=0)),
    }
    checks["warnings"] = [label for key, label in (
        ("boundaryEdges", "열린 경계가 있어 표면 검토가 필요합니다."),
        ("nonManifoldEdges", "한 모서리에 여러 면이 겹쳐 있습니다."),
        ("inconsistentWindingEdges", "이웃 면의 방향이 일치하지 않는 부분이 있습니다."),
        ("duplicateFaces", "중복 면이 있습니다."),
    ) if checks[key]]
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stats", required=True, type=Path)
    parser.add_argument("--target-faces", type=int, default=0, help="0 keeps every face")
    parser.add_argument("--size", type=float, default=1.0, help="longest side in meters, 0 keeps scale")
    parser.add_argument("--audit-dir", type=Path, default=None)
    parser.add_argument("--normalization", type=Path, default=None, help="share the quality master's pivot and scale")
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
    if args.audit_dir:
        mesh.calc_loop_triangles()
        audit_faces = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("vertices", audit_faces)
        args.audit_dir.mkdir(parents=True, exist_ok=True)
        np.savez(args.audit_dir / "post-geometry.npz", vertices=coords,
                 faces=audit_faces.reshape(-1, 3))
    low, high = coords.min(axis=0), coords.max(axis=0)
    dimensions = high - low
    longest = float(dimensions.max())
    scale = args.size / longest if args.size and longest > 0 else 1.0
    offset = Vector((-(low[0] + high[0]) / 2, -(low[1] + high[1]) / 2, -low[2]))
    if args.normalization:
        transform = json.loads(args.normalization.read_text("utf-8"))["normalization"]
        scale, offset = float(transform["scale"]), Vector(transform["offset"])
    mesh.transform(Matrix.Scale(scale, 4) @ Matrix.Translation(offset))
    # 생성된 메시는 중복 면·잘못된 참조를 품고 있어 내보내기가 "유효하지 않다"고 경고한다.
    repaired = mesh.validate(verbose=False)
    mesh.update()
    obj.name = args.output.stem

    mesh.calc_loop_triangles()
    triangles = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    checked_coords = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", checked_coords)
    # Topology inspection does not approve/reject or silently remesh an asset.
    topology = topology_checks(checked_coords.reshape(-1, 3), triangles.reshape(-1, 3))
    if args.target_faces and len(mesh.loop_triangles) > args.target_faces:
        topology["warnings"].append("감축 후에도 요청한 면 수를 넘었습니다.")
    for warning in topology["warnings"]:
        print(f"[mesh warning] {warning}", flush=True)

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
        "normalization": {"scale": scale, "offset": list(offset)},
        "materials": [material.name for material in mesh.materials if material],
        "textures": len(images),
        "bytes": args.output.stat().st_size,
        "topology": topology,
    }
    args.stats.write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")
    print(progress_line(4, 4, f"면 {stats['facesOut']:,}개"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
