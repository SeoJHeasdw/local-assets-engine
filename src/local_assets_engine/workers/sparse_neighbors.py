"""Neighbor pairs for TRELLIS's pure-PyTorch sparse convolution, built with arrays.

The upstream ``conv_none`` backend builds them with a coordinate dict and three Python lists,
which hold a Python object per pair next to the tensors made from them. This builds the same
pairs in the same order (kernel position, then voxel; the last voxel wins for a repeated
coordinate, as the dict does) and puts them in the backend's own spatial cache, so its gather,
matrix multiply and scatter run unchanged on identical inputs. The engine clone is not edited.
"""

from __future__ import annotations

import numpy as np


def neighbor_pairs(coords, kernel, dilation):
    """Source, target and kernel-position indices for ``coords`` of shape [N, 4] (batch, z, y, x)."""
    coords = np.asarray(coords, dtype=np.int64)
    kd, kh, kw = kernel
    dz, dy, dx = dilation
    if not len(coords):
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty.copy(), empty.copy()
    low = coords.min(axis=0)
    extent = coords.max(axis=0) - low + 1
    shifted = coords - low

    def encode(values):
        return ((values[:, 0] * extent[1] + values[:, 1]) * extent[2] + values[:, 2]) * extent[3] + values[:, 3]

    keys = encode(shifted)
    order = np.argsort(keys, kind="stable")
    ordered = keys[order]
    voxels = np.arange(len(coords), dtype=np.int64)
    sources, targets, kernels = [], [], []
    for kz in range(kd):
        for ky in range(kh):
            for kx in range(kw):
                offset = np.array([0, (kz - kd // 2) * dz, (ky - kh // 2) * dy, (kx - kw // 2) * dx])
                moved = shifted + offset
                inside = np.all((moved >= 0) & (moved < extent), axis=1)
                query = encode(moved[inside])
                # The last of equal keys in the stable order is the highest voxel index.
                position = np.searchsorted(ordered, query, side="right") - 1
                found = (position >= 0) & (ordered[np.maximum(position, 0)] == query)
                targets.append(voxels[inside][found])
                sources.append(order[position[found]])
                kernels.append(np.full(int(found.sum()), kz * kh * kw + ky * kw + kx, dtype=np.int64))
    return np.concatenate(sources), np.concatenate(targets), np.concatenate(kernels)


def patch(module) -> None:
    """Fill ``conv_none``'s neighbor cache before its forward would build it with Python lists."""
    import torch

    original = module.sparse_conv3d_forward
    if getattr(original, "__local_assets_patch__", False):
        return

    def sparse_conv3d_forward(self, x):
        _co, kd, kh, kw, _ci = self.weight.shape
        key = f"SubMConv3d_naive_neighbor_{kw}x{kh}x{kd}_dilation{self.dilation}"
        if x.get_spatial_cache(key) is None:
            pairs = neighbor_pairs(x.coords.cpu().numpy(), (kd, kh, kw), self.dilation)
            x.register_spatial_cache(key, tuple(torch.from_numpy(array).to(x.feats.device) for array in pairs))
        return original(self, x)

    sparse_conv3d_forward.__local_assets_patch__ = True
    module.sparse_conv3d_forward = sparse_conv3d_forward
