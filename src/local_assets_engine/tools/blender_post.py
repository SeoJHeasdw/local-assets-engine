"""Normalize a generated mesh for game engines with Blender (bpy).

Joins parts, applies transforms, decimates to a face budget, puts the pivot at
the bottom center, scales the longest side to ``--size`` meters, exports GLB.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import progress_line


# 부품 검사의 범위. 많은 부품·큰 메시에서도 몇 초 안에 끝나도록 묶는다.
PART_PAIR_CANDIDATES = 40
PART_POINTS = 8
PART_WORK = 40_000_000


def winding_numbers(points, triangles):
    """Generalized winding number of each point against a triangle soup (|w| > 0.5 is inside)."""
    import numpy as np

    total = np.zeros(len(points))
    for start in range(0, len(triangles), 100_000):
        chunk = triangles[start:start + 100_000]
        a, b, c = (chunk[None, :, k] - points[:, None] for k in range(3))
        la, lb, lc = (np.linalg.norm(v, axis=2) for v in (a, b, c))
        det = np.einsum("pfi,pfi->pf", a, np.cross(b, c))
        dots = (np.einsum("pfi,pfi->pf", a, b) * lc + np.einsum("pfi,pfi->pf", b, c) * la
                + np.einsum("pfi,pfi->pf", c, a) * lb)
        total += 2 * np.arctan2(det, la * lb * lc + dots).sum(axis=1)
    return total / (4 * np.pi)


def part_checks(vertices, faces, edge_ids, counts):
    """Separate parts: floating away from the main body, closed but inside-out, or inside one another."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    pairs = np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]]))
    graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(vertices),) * 2)
    _, labels = connected_components(graph, directed=False)
    _, part = np.unique(labels[faces[:, 0]], return_inverse=True)
    part = part.reshape(-1)
    total = int(part.max()) + 1
    sizes = np.bincount(part, minlength=total)
    # A part is closed when every one of its edges has exactly two faces.
    open_edge = (counts[edge_ids] != 2).reshape(3, -1).any(axis=0)
    closed = np.bincount(part, weights=open_edge, minlength=total) == 0
    tri = vertices[faces].astype(np.float64)
    volume = np.bincount(part, weights=np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])) / 6,
                         minlength=total)
    low = np.full((total, 3), np.inf)
    high = np.full((total, 3), -np.inf)
    for k in range(3):
        np.minimum.at(low, part, tri[:, k])
        np.maximum.at(high, part, tri[:, k])
    span = float((high.max(axis=0) - low.min(axis=0)).max()) or 1.0
    main = int(np.argmax(sizes))
    gap = np.linalg.norm(np.maximum(0, np.maximum(low - high[main], low[main] - high)), axis=1)
    floating = [i for i in range(total) if i != main and gap[i] > 0.01 * span]
    outward = [int(i) for i in np.argsort(-sizes) if closed[i] and volume[i] > 0]
    reversed_parts = [int(i) for i in np.argsort(-sizes) if closed[i] and volume[i] < -1e-9 * span ** 3]
    # An inside-out part enclosed by an outward one is the inner wall of a hollow (a reconstruction
    # band's inner layer): hidden, not wrong. Only the ones nothing encloses are flipped parts.
    shells, inward, work = [], [], 0
    solids = tri[np.isin(part, outward)]
    if outward:
        from scipy.spatial import cKDTree

        # Quick test: a hidden inner wall sits just behind an outward surface.
        outer_faces = faces[np.isin(part, outward)]
        face_normals = np.cross(solids[:, 1] - solids[:, 0], solids[:, 2] - solids[:, 0])
        normals = np.zeros((len(vertices), 3))
        for k in range(3):
            np.add.at(normals, outer_faces[:, k], face_normals)
        surface = np.unique(outer_faces.reshape(-1))
        tree = cKDTree(vertices[surface])
    for index in reversed_parts:
        corners = np.unique(faces[part == index].reshape(-1))
        sample = vertices[corners[[0, len(corners) // 2]]].astype(np.float64)
        enclosed = False
        if outward:
            distance, nearest = tree.query(sample)
            anchor = surface[nearest]
            behind = np.einsum("ij,ij->i", sample - vertices[anchor], normals[anchor]) < 0
            enclosed = bool(np.all(behind & (distance < 0.05 * span)))
            if not enclosed and work + 2 * len(solids) <= PART_WORK:
                work += 2 * len(solids)
                enclosed = bool(np.all(np.abs(winding_numbers(sample, solids)) > 0.5))
        (shells if enclosed else inward).append(index)
    candidates = [i for i in outward if sizes[i] >= 4][:PART_PAIR_CANDIDATES]
    intersecting, work = [], 0
    for x, outer in enumerate(candidates):
        for inner in candidates[x + 1:]:
            if np.any(low[inner] > high[outer]) or np.any(low[outer] > high[inner]):
                continue
            work += PART_POINTS * sizes[outer]
            if work > PART_WORK:
                break
            corners = np.unique(faces[part == inner].reshape(-1))
            sample = vertices[corners[np.linspace(0, len(corners) - 1, PART_POINTS).astype(int)]].astype(np.float64)
            if np.any(np.abs(winding_numbers(sample, tri[part == outer])) > 0.5):
                intersecting.append((outer, inner))

    def describe(index):
        return {"faces": int(sizes[index]), "center": np.round((low[index] + high[index]) / 2, 4).tolist(),
                "size": np.round(high[index] - low[index], 4).tolist()}

    return {
        "parts": total, "smallParts": int((sizes <= 10).sum()),
        "floatingParts": len(floating), "inwardParts": len(inward), "intersectingParts": len(intersecting),
        "innerShells": len(shells), "innerShellFaceShare": round(float(sizes[shells].sum() / sizes.sum()), 4) if shells else 0.0,
        "partDetails": {"floating": [describe(i) for i in floating[:10]], "inward": [describe(i) for i in inward[:10]],
                        "intersecting": [[describe(a), describe(b)] for a, b in intersecting[:10]]},
    }


def topology_checks(vertices, triangles):
    """Inspect geometric adjacency across UV seams, without changing the mesh."""
    import numpy as np

    # Exporters split vertices at UV/normal seams. Exact coincident positions
    # should count as one vertex for inspection, but must retain their UVs.
    welded, inverse = np.unique(vertices, axis=0, return_inverse=True)
    faces = inverse.reshape(-1)[triangles]
    edges = np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    canonical = np.sort(edges, axis=1)
    _, edge_ids, counts = np.unique(canonical, axis=0, return_inverse=True, return_counts=True)
    edge_ids = edge_ids.reshape(-1)
    direction = np.where(edges[:, 0] < edges[:, 1], 1, -1)
    balance = np.bincount(edge_ids, weights=direction, minlength=len(counts))
    checks = {
        "boundaryEdges": int(np.count_nonzero(counts == 1)),
        "nonManifoldEdges": int(np.count_nonzero(counts > 2)),
        "inconsistentWindingEdges": int(np.count_nonzero((counts == 2) & (balance != 0))),
        "duplicateFaces": len(faces) - len(np.unique(np.sort(faces, axis=1), axis=0)),
        **part_checks(welded, faces, edge_ids, counts),
    }
    # 부품 신호는 장식·부속일 수도 있다. 지우지 않고 사람이 보도록 알린다.
    checks["warnings"] = [label for key, label in (
        ("boundaryEdges", "열린 경계가 있어 표면 검토가 필요합니다."),
        ("nonManifoldEdges", "한 모서리에 여러 면이 겹쳐 있습니다."),
        ("inconsistentWindingEdges", "이웃 면의 방향이 일치하지 않는 부분이 있습니다."),
        ("duplicateFaces", "중복 면이 있습니다."),
        ("floatingParts", "본체와 떨어져 떠 있는 조각이 있습니다."),
        ("inwardParts", "면이 안쪽을 향하는 닫힌 부품이 있습니다."),
        ("intersectingParts", "서로 파고들거나 안에 든 닫힌 부품이 있습니다."),
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
