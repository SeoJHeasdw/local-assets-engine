"""Exact inference dual-grid connectivity with bounded CPU array batches."""

from __future__ import annotations

import numpy as np

OFFSETS = np.array([
    [[0, 0, 0], [0, 0, 1], [0, 1, 1], [0, 1, 0]],
    [[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]],
    [[0, 0, 0], [0, 1, 0], [1, 1, 0], [1, 0, 0]],
], dtype=np.int64)


def extract_faces(coords, flags, weights, batch_size=131072):
    coords = np.asarray(coords, dtype=np.int64)
    if not len(coords):
        return np.empty((0, 3), dtype=np.int64)
    low = coords.min(axis=0)
    spans = coords.max(axis=0) - low + 2  # include queried +1 neighbors

    def keys(points):
        p = points - low
        return (p[..., 0] * spans[1] + p[..., 1]) * spans[2] + p[..., 2]

    encoded = keys(coords)
    order = np.argsort(encoded, kind="stable")
    sorted_keys = encoded[order]
    if np.any(sorted_keys[1:] == sorted_keys[:-1]):
        raise ValueError("Dual-grid coordinates must be unique")
    split_a, split_b = [0, 1, 2, 0, 2, 3], [0, 1, 3, 3, 1, 2]
    output = []
    for start in range(0, len(coords), batch_size):
        neighbors = (coords[start:start + batch_size, None, None, :] + OFFSETS)[flags[start:start + batch_size]]
        query = keys(neighbors)
        indices = np.searchsorted(sorted_keys, query)
        indices = np.minimum(indices, len(sorted_keys) - 1)
        valid = (sorted_keys[indices] == query).all(axis=1)
        quad = order[indices[valid]]
        sw = weights[quad]
        choose = (sw[:, 0] * sw[:, 2] > sw[:, 1] * sw[:, 3]).reshape(-1, 1)
        output.append(np.where(choose, quad[:, split_a], quad[:, split_b]).reshape(-1, 3))
    return np.concatenate(output)


def patch(module):
    """Keep the upstream training/normal-split paths; patch inference only."""
    import torch

    original = module.flexible_dual_grid_to_mesh

    def convert(coords, dual_vertices, intersected_flag, split_weight, aabb,
                voxel_size=None, grid_size=None, train=False):
        if train or split_weight is None:
            return original(coords, dual_vertices, intersected_flag, split_weight,
                            aabb, voxel_size=voxel_size, grid_size=grid_size, train=train)
        faces = extract_faces(coords.cpu().numpy(), intersected_flag.cpu().numpy(),
                              split_weight.float().cpu().numpy())
        if not len(faces):
            return (torch.zeros((0, 3), device=coords.device),
                    torch.zeros((0, 3), dtype=torch.long, device=coords.device))
        box = torch.as_tensor(aabb, dtype=torch.float32, device=coords.device)
        size = (torch.as_tensor(voxel_size, dtype=torch.float32, device=coords.device)
                if voxel_size is not None else
                (box[1] - box[0]) / torch.as_tensor(grid_size, dtype=torch.float32, device=coords.device))
        vertices = (coords.float() + dual_vertices) * size + box[0]
        return vertices, torch.from_numpy(faces).to(coords.device)

    module.flexible_dual_grid_to_mesh = convert
