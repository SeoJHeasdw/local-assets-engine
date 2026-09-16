"""Better texture baking for the fallback path that runs without Metal.

trellis-mac's fallback baker gives each texel the inverse-distance weighted
colour of its eight nearest voxels, leaves texels with no voxel within two
voxel widths black, and then fills those holes by blurring - which mixes the
black back into the neighbours. Every surface ends up speckled with dark
texels.

Two changes fix that without a Metal rasterizer: search a wider neighbourhood,
and fill holes from the nearest texel that actually has a colour.

This file runs inside the TRELLIS environment (Python 3.11), so it must not
import ``local_assets_engine``.
"""

from __future__ import annotations

import time

import numpy as np

# 범위를 넓히면 구멍은 줄지만 이웃한 재질(나무와 철 띠)의 색이 섞여 탈색된다.
# 구멍은 이제 가장 가까운 유효 텍셀로 메우므로, 범위는 좁고 가중치는 가파르게 둔다.
DEFAULT_MAX_DIST_VOXELS = 2.5
DEFAULT_NEIGHBORS = 6
DEFAULT_POWER = 2.0


def sample_voxels(distances, indices, attrs, voxel_size,
                  max_dist_voxels=DEFAULT_MAX_DIST_VOXELS, power=DEFAULT_POWER):
    """Distance weighted colour per texel, ignoring voxels that are too far."""
    eps = voxel_size * 0.1
    weights = 1.0 / np.power(distances + eps, power)
    weights[distances > voxel_size * max_dist_voxels] = 0.0
    total = weights.sum(axis=1, keepdims=True)
    has_neighbor = (total > 0).reshape(-1)
    weights = np.where(total > 0, weights / np.maximum(total, 1e-10), 0.0)
    return (attrs[indices] * weights[..., None]).sum(axis=1), has_neighbor


def fill_holes(image, valid):
    """Give every empty texel the colour of the nearest texel that has one."""
    from scipy.ndimage import distance_transform_edt

    if not valid.any() or valid.all():
        return image
    _, (rows, columns) = distance_transform_edt(~valid, return_indices=True)
    return image[rows, columns]


def make_bake_texture(module):
    """Build a replacement for ``module.bake_texture`` reusing its rasterizer."""
    rasterize = getattr(module, "_rasterize_uv_triangles")

    def bake_texture(vertices, faces, uvs, voxel_coords, voxel_attrs, origin, voxel_size,
                     texture_size=1024, k_neighbors=DEFAULT_NEIGHBORS, **kwargs):
        from scipy.spatial import cKDTree

        started = time.time()
        coords = voxel_coords.numpy() if hasattr(voxel_coords, "numpy") else np.asarray(voxel_coords)
        attrs = voxel_attrs.numpy() if hasattr(voxel_attrs, "numpy") else np.asarray(voxel_attrs)
        origin_np = origin.numpy() if hasattr(origin, "numpy") else np.asarray(origin)
        voxel_world = coords.astype(np.float32) * voxel_size + origin_np + voxel_size * 0.5

        positions, mask = rasterize(vertices, faces, uvs, texture_size)
        tree = cKDTree(voxel_world)
        distances, indices = tree.query(positions[mask], k=max(k_neighbors, 1), workers=-1)
        sampled, has_neighbor = sample_voxels(distances, indices, attrs, voxel_size)

        height = width = texture_size
        base_color = np.zeros((height, width, 3), dtype=np.float32)
        metallic = np.zeros((height, width), dtype=np.float32)
        roughness = np.ones((height, width), dtype=np.float32)
        rows, columns = np.where(mask)
        rows, columns = rows[has_neighbor], columns[has_neighbor]
        channels = attrs.shape[1]
        base_color[rows, columns] = np.clip(sampled[has_neighbor, 0:3], 0, 1)
        if channels > 3:
            metallic[rows, columns] = np.clip(sampled[has_neighbor, 3], 0, 1)
        if channels > 4:
            roughness[rows, columns] = np.clip(sampled[has_neighbor, 4], 0, 1)

        filled = np.zeros((height, width), dtype=bool)
        filled[rows, columns] = True
        print(f"  [local-assets] 색을 받은 텍셀 {filled.sum() / filled.size * 100:.1f}%"
              f" (마스크 {mask.sum() / mask.size * 100:.1f}%)", flush=True)

        base_color = fill_holes(np.power(np.clip(base_color, 0, 1), 1.0 / 2.2), filled)
        metallic = fill_holes(metallic[..., None], filled)[..., 0]
        roughness = fill_holes(roughness[..., None], filled)[..., 0]

        metallic_roughness = np.zeros((height, width, 3), dtype=np.uint8)
        metallic_roughness[:, :, 1] = (roughness * 255).astype(np.uint8)
        metallic_roughness[:, :, 2] = (metallic * 255).astype(np.uint8)
        print(f"  [local-assets] 굽기 {time.time() - started:.0f}초", flush=True)
        return (base_color * 255).astype(np.uint8), metallic_roughness, mask

    return bake_texture


def patch(module) -> None:
    module.bake_texture = make_bake_texture(module)
