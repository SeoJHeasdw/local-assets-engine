"""Unwrap final geometry and bake native PBR from the preserved model surface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np


def progress(value, detail):
    print("@@progress " + json.dumps({"done": value, "total": 100, "detail": detail}, ensure_ascii=False), flush=True)


class SparseSampler:
    """Trilinear lookup using the same grid coordinates as TRELLIS's exporter.

    Missing corners are renormalized, rather than mixed with black. Queries
    with no valid corner fall back to the nearest generated voxel.
    """

    def __init__(self, coords, attrs, origin, voxel_size):
        from scipy.spatial import cKDTree

        self.coords = np.asarray(coords, dtype=np.int64)
        self.attrs = np.asarray(attrs, dtype=np.float32)
        self.origin = np.asarray(origin, dtype=np.float32)
        self.step = float(voxel_size)
        self.low = self.coords.min(axis=0) - 2
        self.high = self.coords.max(axis=0) + 2
        self.spans = self.high - self.low + 1
        keys = self.keys(self.coords)
        self.order = np.argsort(keys)
        self.sorted_keys = keys[self.order]
        self.tree = cKDTree(self.coords)

    def keys(self, coords):
        p = coords - self.low
        return (p[..., 0] * self.spans[1] + p[..., 1]) * self.spans[2] + p[..., 2]

    def sample(self, positions):
        grid = (positions - self.origin) / self.step
        floor = np.floor(grid).astype(np.int64)
        fraction = grid - floor
        total = np.zeros(len(grid), dtype=np.float32)
        value = np.zeros((len(grid), self.attrs.shape[1]), dtype=np.float32)
        for x in (0, 1):
            for y in (0, 1):
                for z in (0, 1):
                    corner = np.array([x, y, z])
                    coords = floor + corner
                    weight = np.prod(np.where(corner, fraction, 1 - fraction), axis=1)
                    query = self.keys(coords)
                    index = np.minimum(np.searchsorted(self.sorted_keys, query), len(self.sorted_keys) - 1)
                    valid = (self.sorted_keys[index] == query) & ((coords >= self.low) & (coords <= self.high)).all(axis=1)
                    weight *= valid
                    value += self.attrs[self.order[index]] * weight[:, None]
                    total += weight
        present = total > 1e-7
        value[present] /= total[present, None]
        if (~present).any():
            _, index = self.tree.query(grid[~present], workers=-1)
            value[~present] = self.attrs[index]
        return np.clip(value, 0, 1), int((~present).sum())


def rasterize(vertices, faces, uvs, size):
    """Texel-center rasterization; image row V and glTF V use one convention."""
    positions = np.zeros((size, size, 3), dtype=np.float32)
    mask = np.zeros((size, size), dtype=bool)
    for fi, indices in enumerate(faces):
        uv = uvs[indices] * size - 0.5
        p = vertices[indices]
        low = np.maximum(np.ceil(uv.min(axis=0)).astype(int), 0)
        high = np.minimum(np.floor(uv.max(axis=0)).astype(int), size - 1)
        if np.any(high < low):
            continue
        edge1, edge2 = uv[1] - uv[0], uv[2] - uv[0]
        det = edge1[0] * edge2[1] - edge1[1] * edge2[0]
        if abs(det) < 1e-10:
            continue
        x, y = np.meshgrid(np.arange(low[0], high[0] + 1), np.arange(low[1], high[1] + 1))
        dx, dy = x - uv[0, 0], y - uv[0, 1]
        b = (dx * edge2[1] - dy * edge2[0]) / det
        c = (edge1[0] * dy - edge1[1] * dx) / det
        a = 1 - b - c
        inside = (a >= -1e-6) & (b >= -1e-6) & (c >= -1e-6)
        rows, cols = y[inside], x[inside]
        positions[rows, cols] = (a[inside, None] * p[0] + b[inside, None] * p[1] + c[inside, None] * p[2])
        mask[rows, cols] = True
        if fi % 100000 == 0:
            progress(20 + 30 * fi / len(faces), "텍스처 좌표에 표면 배치")
    return positions, mask


def bake(source, geometry, output, texture_size=4096):
    import point_cloud_utils as pcu
    import trimesh
    import xatlas
    from scipy.ndimage import distance_transform_edt
    from gltf_export import export_glb_with_texture

    start = time.perf_counter()
    data = np.load(source, allow_pickle=False)
    model_vertices = np.asarray(data["vertices"], dtype=np.float32)
    model_faces = np.asarray(data["faces"], dtype=np.int32)
    geom = np.load(geometry, allow_pickle=False)
    vertices, faces = np.asarray(geom["vertices"], dtype=np.float32), np.asarray(geom["faces"], dtype=np.uint32)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    normals = np.asarray(mesh.vertex_normals, dtype=np.float32)
    progress(0, "최종 형상에 UV 펼치기")
    atlas = xatlas.Atlas()
    atlas.add_mesh(vertices, faces, normals=normals)
    pack = xatlas.PackOptions()
    pack.resolution = texture_size
    pack.padding = 4
    pack.bilinear = True
    atlas.generate(pack_options=pack)
    mapping, atlas_faces, uv = atlas[0]
    atlas_vertices = vertices[mapping]
    uv_seconds = time.perf_counter() - start
    progress(20, "UV 완료 · 원본 표면에 색 맞추기")
    positions, mask = rasterize(atlas_vertices, atlas_faces, uv, texture_size)
    raster_seconds = time.perf_counter() - start - uv_seconds
    sampler = SparseSampler(data["coords"], data["attrs"], data["origin"], data["voxel_size"])
    indices = np.flatnonzero(mask)
    colors = np.zeros((texture_size * texture_size, 6), dtype=np.float32)
    samples = positions.reshape(-1, 3)
    fallback, maximum_distance = 0, 0.0
    for begin in range(0, len(indices), 1_000_000):
        selected = indices[begin:begin + 1_000_000]
        distance, face_id, barycentric = pcu.closest_points_on_mesh(samples[selected], model_vertices, model_faces)
        projected = (model_vertices[model_faces[face_id]] * barycentric[..., None]).sum(axis=1)
        for offset in range(0, len(selected), 131072):
            part = selected[offset:offset + 131072]
            values, missed = sampler.sample(projected[offset:offset + len(part)])
            colors[part, :values.shape[1]] = values
            if values.shape[1] < 6:
                colors[part, 5] = 1
            fallback += missed
        maximum_distance = max(maximum_distance, float(distance.max(initial=0)))
        progress(50 + 40 * (begin + len(selected)) / len(indices), "원본 PBR 재질 굽기")
    if not len(indices):
        raise ValueError("UV 아틀라스에 유효한 텍셀이 없습니다.")
    # Dilation across chart padding prevents filtering from mixing with black.
    nearest = distance_transform_edt(~mask, return_distances=False, return_indices=True)
    filled = colors.reshape(texture_size, texture_size, 6)[nearest[0], nearest[1]]
    base_color = (filled[..., [0, 1, 2, 5]] * 255).round().astype(np.uint8)
    mr = np.zeros((texture_size, texture_size, 3), dtype=np.uint8)
    mr[..., 1], mr[..., 2] = (filled[..., 4] * 255).round(), (filled[..., 3] * 255).round()
    output.parent.mkdir(parents=True, exist_ok=True)
    export_glb_with_texture(atlas_vertices, atlas_faces, uv, base_color, mr, output,
                            normals=normals[mapping], native_pbr=True)
    report = {"version": 2, "triangles": len(atlas_faces), "textureSize": texture_size,
              "uvSeconds": uv_seconds, "rasterSeconds": raster_seconds,
              "seconds": time.perf_counter() - start, "validTexels": len(indices),
              "fallbackTexels": fallback, "maxProjectionDistance": maximum_distance,
              "colorSpace": "sRGB", "sampling": "source-projection+sparse-trilinear"}
    output.with_suffix(".bake.json").write_text(json.dumps(report, indent=2), "utf-8")
    progress(100, "PBR GLB 저장 완료")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--geometry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--texture-size", type=int, default=4096, choices=(512, 1024, 2048, 4096))
    args = parser.parse_args()
    bake(args.source, args.geometry, args.output, args.texture_size)
