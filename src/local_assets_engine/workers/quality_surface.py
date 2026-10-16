"""CPU unsigned-distance reconstruction before decimation and UV generation.

Uses the full TRELLIS surface. A narrow band gives unsigned/open surfaces a
consistent orientation without filling every enclosed cavity. No model loads.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np


def progress(done, total, detail):
    print("@@progress " + json.dumps({"done": done, "total": total, "detail": detail}, ensure_ascii=False), flush=True)


def clean_mesh(vertices, faces, merge=True):
    """Remove only exact duplicates/unusable primitives; retain all components.

    ``merge=False`` keeps vertices that only share a position. Marching cubes output is
    already welded; merging two of its vertices that float32 rounded onto one point
    turns a manifold surface non-manifold, and the decimator then returns nothing.
    """
    vertices = np.asarray(vertices, dtype=np.float32)
    faces = np.asarray(faces, dtype=np.int64)
    if not np.isfinite(vertices).all() or not len(faces):
        raise ValueError("표면에 유효한 유한 좌표와 삼각형이 필요합니다.")
    if merge:
        vertices, inverse = np.unique(vertices, axis=0, return_inverse=True)
        faces = inverse.reshape(-1)[faces]
    nonzero = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 2] != faces[:, 0])
    faces = faces[nonzero]
    if merge:
        triangle = vertices[faces]
        area = np.linalg.norm(np.cross(triangle[:, 1] - triangle[:, 0], triangle[:, 2] - triangle[:, 0]), axis=1)
        faces = faces[area > 0]
    _, keep = np.unique(np.sort(faces, axis=1), axis=0, return_index=True)
    faces = faces[np.sort(keep)]
    used, indices = np.unique(faces, return_inverse=True)
    return vertices[used], indices.reshape(-1, 3).astype(np.int32)


def topology(vertices, faces):
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    signs = np.where(edges[:, 0] < edges[:, 1], 1, -1)
    edges = np.sort(edges, axis=1)
    keys = edges[:, 0].astype(np.int64) * len(vertices) + edges[:, 1]
    _, inverse, count = np.unique(keys, return_inverse=True, return_counts=True)
    balance = np.bincount(inverse, weights=signs)
    return {"boundaryEdges": int((count == 1).sum()), "nonManifoldEdges": int((count > 2).sum()),
            "inconsistentWindingEdges": int(((count == 2) & (balance != 0)).sum())}


def significant_parts(vertices, faces, share=0.002, minimum=200):
    """Connected parts that carry at least ``share`` of the area and ``minimum`` faces."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    _, inverse = np.unique(vertices, axis=0, return_inverse=True)
    welded = inverse.reshape(-1)[faces]
    edges = np.concatenate([welded[:, [0, 1]], welded[:, [1, 2]]])
    size = int(welded.max()) + 1
    graph = coo_matrix((np.ones(len(edges)), (edges[:, 0], edges[:, 1])), shape=(size, size))
    _, labels = connected_components(graph, directed=False)
    part = labels[welded[:, 0]]
    triangle = vertices[faces]
    area = np.linalg.norm(np.cross(triangle[:, 1] - triangle[:, 0], triangle[:, 2] - triangle[:, 0]), axis=1) / 2
    areas, counts = np.bincount(part, weights=area), np.bincount(part)
    return int(((areas >= share * area.sum()) & (counts >= minimum)).sum())


def project_surface(vertices, faces, source_vertices, source_faces, amount=0.7):
    """Recover sub-voxel detail while refusing inverted/collapsed triangles."""
    import point_cloud_utils as pcu

    _, index, barycentric = pcu.closest_points_on_mesh(vertices, source_vertices, source_faces)
    nearest = (source_vertices[source_faces[index]] * barycentric[..., None]).sum(axis=1)
    projected = (vertices + amount * (nearest - vertices)).astype(np.float32)
    tri = vertices[faces]
    old_normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    old_area = np.linalg.norm(old_normal, axis=1)
    # Each iteration reverts a superset; terminate when neighbors are safe too.
    for _ in range(20):
        tri = projected[faces]
        new_normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        new_area = np.linalg.norm(new_normal, axis=1)
        bad = ((new_normal * old_normal).sum(axis=1) < 0.2 * old_area * new_area) | (new_area < old_area * 0.1)
        if not bad.any():
            return projected
        affected = np.unique(faces[bad])
        projected[affected] = vertices[affected]
    # The original surface is a valid, preserved fallback for a folded region.
    return vertices.copy()


def reconstruct(source, output, target_faces=1_000_000, game_faces=100_000):
    import point_cloud_utils as pcu
    from scipy import ndimage
    from skimage.measure import marching_cubes

    started = time.perf_counter()
    data = np.load(source, allow_pickle=False)
    original_vertices = np.asarray(data["vertices"], dtype=np.float32)
    original_faces = np.asarray(data["faces"], dtype=np.int32)
    coords = np.asarray(data["coords"], dtype=np.int32)
    origin = np.asarray(data["origin"], dtype=np.float32)
    step = float(data["voxel_size"])
    resolution = round(1 / step)
    if resolution > 1024 or resolution < 16:
        raise ValueError("지원하지 않는 원본 복셀 해상도입니다.")
    output.mkdir(parents=True, exist_ok=True)
    report = {"version": 1, "method": "unsigned-distance-band", "resolution": resolution,
              "sourceVertices": len(original_vertices), "sourceTriangles": len(original_faces), "variants": {}}
    # Crop to occupied bounds instead of allocating an entire 1024^3 volume.
    pad = 3
    low, high = coords.min(axis=0) - pad, coords.max(axis=0) + pad
    shape = high - low + 1
    if np.prod(shape, dtype=np.int64) > 600_000_000:
        raise ValueError("재구성 부피가 메모리 예산을 넘습니다. 원본은 보존되었습니다.")
    active = np.zeros(tuple(shape), dtype=bool)
    active[tuple((coords - low).T)] = True
    active = ndimage.binary_dilation(active, iterations=2)
    cells = np.column_stack(np.nonzero(active)).astype(np.int32)
    del active
    field = np.full(tuple(shape), step * 4, dtype=np.float32)
    progress(0, 100, "원본 표면과의 거리 계산")
    for start in range(0, len(cells), 1_000_000):
        selected = cells[start:start + 1_000_000]
        points = ((selected + low) * step + origin).astype(np.float32)
        distance, _, _ = pcu.closest_points_on_mesh(points, original_vertices, original_faces)
        field[tuple(selected.T)] = distance
        progress(50 * (start + len(selected)) / len(cells), 100, "원본 표면과의 거리 계산")
    del cells
    vertices, faces, _, _ = marching_cubes(field, level=step * 1.2, spacing=(step,) * 3, allow_degenerate=False)
    vertices = (vertices + low * step + origin).astype(np.float32)
    del field
    vertices, faces = clean_mesh(vertices, faces, merge=False)
    report["warnings"] = []
    # 가까운 부품의 띠가 합쳐지면 좁은 틈이 메워진다. 의미 있는 부품 수가 줄었는지 알린다.
    parts = {"source": significant_parts(*clean_mesh(original_vertices, original_faces)),
             "reconstructed": significant_parts(vertices, faces)}
    report["parts"] = parts
    if parts["reconstructed"] < parts["source"]:
        report["warnings"].append(f"떨어져 있던 부품 {parts['source']}개가 재구성 후 {parts['reconstructed']}개로 "
                                  "붙었습니다. 좁은 틈이 메워졌을 수 있습니다.")
    report["reconstructedTriangles"] = len(faces)
    report["reconstructedTopology"] = topology(vertices, faces)
    if report["reconstructedTopology"]["nonManifoldEdges"] or report["reconstructedTopology"]["boundaryEdges"]:
        # 열린 경계나 세 면 이상이 붙은 모서리가 있으면 감축기가 빈 결과를 내거나 메모리를 끝없이 쓴다.
        raise SystemExit(f"재구성 표면이 닫혀 있지 않아 형상 보존 감축을 할 수 없습니다: "
                         f"{report['reconstructedTopology']}. 원본은 보존되었습니다.")
    np.savez(output / "surface-full.npz", vertices=vertices, faces=faces)
    progress(60, 100, "표면 재구성 완료 · 형상 보존 감축")
    for index, (name, budget) in enumerate((("master", target_faces), ("game", game_faces))):
        if name == "game" and budget == 0:
            continue
        if budget and len(faces) > budget:
            # The fast QEM implementation can turn a manifold input into
            # non-manifold edges. Preserve topology even when it takes longer.
            try:
                reduced_v, reduced_f, _, _ = pcu.decimate_triangle_mesh(vertices, faces, budget)
            except ValueError as error:
                raise SystemExit(f"형상 보존 감축이 실패했습니다. 원본은 보존되었습니다. ({error})") from error
            reduced_v, reduced_f = clean_mesh(reduced_v, reduced_f)
        else:
            reduced_v, reduced_f = vertices.copy(), faces.copy()
        reduced_v = project_surface(reduced_v, reduced_f, original_vertices, original_faces)
        reduced_v, reduced_f = clean_mesh(reduced_v, reduced_f)
        checks = topology(reduced_v, reduced_f)
        np.savez(output / f"{name}.npz", vertices=reduced_v, faces=reduced_f)
        report["variants"][name] = {"targetFaces": budget, "vertices": len(reduced_v),
                                    "triangles": len(reduced_f), "topology": checks}
        progress(80 + 15 * index, 100, f"{name} · {len(reduced_f):,}면")
    report["seconds"] = time.perf_counter() - started
    (output / "surface.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    progress(100, 100, "표면 재구성 완료")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-faces", type=int, default=1_000_000)
    parser.add_argument("--game-faces", type=int, default=100_000)
    args = parser.parse_args()
    reconstruct(args.source, args.output, args.target_faces, args.game_faces)
