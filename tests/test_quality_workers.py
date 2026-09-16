import numpy as np
import pytest

from local_assets_engine.workers.mesh_extract import extract_faces, OFFSETS
from local_assets_engine.workers.quality_texture import SparseSampler, rasterize
from local_assets_engine.workers.quality_surface import clean_mesh, topology


def test_array_extraction_matches_independent_connectivity_and_chunk_order():
    coords = np.array([[x, y, z] for x in range(-2, 3) for y in range(3) for z in range(2)])
    coords = np.delete(coords, [4, 13], axis=0)
    rng = np.random.default_rng(123)
    weights = rng.random((len(coords), 1), dtype=np.float32)
    flags = rng.random((len(coords), 3)) > 0.3
    lookup = {tuple(p): i for i, p in enumerate(coords)}
    expected = []
    for i, p in enumerate(coords):
        for axis in range(3):
            neighbors = [lookup.get(tuple(p + delta)) for delta in OFFSETS[axis]]
            if not flags[i, axis] or None in neighbors:
                continue
            w = weights[neighbors, 0]
            split = [0, 1, 2, 0, 2, 3] if w[0] * w[2] > w[1] * w[3] else [0, 1, 3, 3, 1, 2]
            expected.extend(np.asarray(neighbors)[split].reshape(-1, 3))
    for batch in (1, 7, 100):
        np.testing.assert_array_equal(extract_faces(coords, flags, weights, batch), expected)
    assert extract_faces(coords, flags & False, weights).shape == (0, 3)


def test_trilinear_sampler_does_not_shift_or_gamma_correct_generated_colors():
    coords = np.array([[x, y, z] for x in range(2) for y in range(2) for z in range(2)])
    sampler = SparseSampler(coords, coords.astype(np.float32), [-0.5] * 3, 0.5)
    colors, fallback = sampler.sample(np.array([[-0.5, -0.5, -0.5], [-0.25, -0.25, -0.25], [0, 0, 0]]))
    np.testing.assert_allclose(colors, [[0, 0, 0], [0.5, 0.5, 0.5], [1, 1, 1]])
    assert fallback == 0


def test_sparse_missing_corners_do_not_mix_with_black_or_wrap_grid_keys():
    sampler = SparseSampler(np.array([[0, 0, 0]]), np.array([[0.8, 0.4, 0.2]]), [0, 0, 0], 1)
    colors, fallback = sampler.sample(np.array([[0.5, 0.5, 0.5], [0, 5, 0]]))
    np.testing.assert_allclose(colors, [[0.8, 0.4, 0.2], [0.8, 0.4, 0.2]])
    assert fallback == 1


def test_texel_center_rasterization_matches_known_planar_surface():
    vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
    positions, mask = rasterize(vertices, np.array([[0, 1, 2]]), vertices[:, :2], 4)
    assert mask[0, 0] and not mask[3, 3]
    np.testing.assert_allclose(positions[0, 0], [0.125, 0.125, 0])
    np.testing.assert_allclose(positions[1, 1], [0.375, 0.375, 0])


def test_cleaning_retains_tiny_valid_triangles_but_removes_exact_duplicates():
    vertices = np.array([[0., 0, 0], [1e-8, 0, 0], [0, 1e-8, 0], [0, 0, 0]])
    v, f = clean_mesh(vertices, np.array([[0, 1, 2], [3, 1, 2], [0, 0, 1]]))
    assert len(f) == 1
    assert topology(v, f)["boundaryEdges"] == 3
    with pytest.raises(ValueError):
        clean_mesh(vertices * np.nan, np.array([[0, 1, 2]]))
