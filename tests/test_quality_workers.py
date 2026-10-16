import numpy as np
import pytest

from local_assets_engine.workers.mesh_extract import extract_faces, OFFSETS
from local_assets_engine.workers.quality_texture import SparseSampler, pad, quantize, rasterize
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


def test_cleaning_can_keep_vertices_that_only_share_a_position():
    # Marching cubes output is welded already. Merging two of its vertices that float32 put on
    # one point can make an edge non-manifold, after which the decimator returns nothing.
    vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 1, 0]], dtype=np.float32)
    faces = np.array([[0, 1, 2], [0, 3, 1]])
    merged_v, merged_f = clean_mesh(vertices, faces)
    kept_v, kept_f = clean_mesh(vertices, faces, merge=False)
    assert len(merged_v) == 3 and len(kept_v) == 4 and len(kept_f) == 2


def test_each_pipeline_type_loads_only_the_models_it_samples_with():
    from local_assets_engine.workers.trellis_infer import models_for

    names = ["sparse_structure_flow_model", "sparse_structure_decoder", "shape_slat_flow_model_512",
             "shape_slat_flow_model_1024", "shape_slat_decoder", "tex_slat_flow_model_512",
             "tex_slat_flow_model_1024", "tex_slat_decoder"]
    shared = {"sparse_structure_flow_model", "sparse_structure_decoder", "shape_slat_decoder", "tex_slat_decoder"}
    assert set(models_for(names, "512")) == shared | {"shape_slat_flow_model_512", "tex_slat_flow_model_512"}
    assert set(models_for(names, "1024")) == shared | {"shape_slat_flow_model_1024", "tex_slat_flow_model_1024"}
    # The cascade samples shape at 512 then 1024 and texture at 1024 only.
    assert set(models_for(names, "1024_cascade")) == shared | {
        "shape_slat_flow_model_512", "shape_slat_flow_model_1024", "tex_slat_flow_model_1024"}


def test_array_neighbor_pairs_match_the_upstream_sparse_convolution_exactly():
    import subprocess
    from pathlib import Path

    from local_assets_engine.paths import trellis_python

    if not trellis_python().exists():
        pytest.skip("TRELLIS 환경이 설치되지 않았다")
    root = Path(__file__).resolve().parents[1]
    script = f"""
import sys, torch
sys.path.insert(0, {str(root / 'src/local_assets_engine/workers')!r})
sys.path.insert(0, {str(root / 'engines/trellis-mac/TRELLIS.2')!r})
from trellis2.modules.sparse.conv import conv_none as upstream
import sparse_neighbors

class Tensor:
    def __init__(self, coords, feats):
        self.coords, self.feats, self.cache = coords, feats, {{}}
    def get_spatial_cache(self, key): return self.cache.get(key)
    def register_spatial_cache(self, key, value): self.cache[key] = value
    def replace(self, out): return out

class Layer:
    def __init__(self, dilation):
        torch.manual_seed(0)
        self.weight = torch.randn(5, 3, 3, 3, 4)
        self.bias = None
        self.dilation = dilation

g = torch.Generator().manual_seed(7)
coords = torch.unique(torch.cat([torch.zeros(400, 1, dtype=torch.int32), torch.randint(0, 9, (400, 3), generator=g, dtype=torch.int32)], 1), dim=0)
second = coords.clone(); second[:, 0] = 1
coords = torch.cat([coords, second])[torch.randperm(2 * len(coords), generator=g)]
feats = torch.randn(len(coords), 4, generator=g)
original = upstream.sparse_conv3d_forward
for dilation in ((1, 1, 1), (2, 1, 2)):
    layer = Layer(dilation)
    a, b = Tensor(coords, feats), Tensor(coords, feats)
    out_a = original(layer, a)
    sparse_neighbors.patch(upstream)
    out_b = upstream.sparse_conv3d_forward(layer, b)
    upstream.sparse_conv3d_forward = original
    (ka, va), = a.cache.items(); (kb, vb), = b.cache.items()
    assert ka == kb and all(torch.equal(x, y) for x, y in zip(va, vb)), dilation
    assert torch.equal(out_a, out_b), dilation
    print("same", dilation, len(va[0]))
"""
    done = subprocess.run([str(trellis_python()), "-c", script], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.count("same") == 2



@pytest.mark.parametrize("channels", [6, 5])
def test_chunked_quantization_and_in_place_padding_match_the_float_texture_bytes(channels):
    from scipy.ndimage import distance_transform_edt

    size = 256
    rows, cols = np.arange(size)[:, None], np.arange(size)[None, :]
    mask = ((rows % 13) != 0) & ((cols % 17) != 0) & ((rows + cols) % 29 != 0)
    indices = np.flatnonzero(mask)
    values = np.random.default_rng(3).random((len(indices), channels), dtype=np.float32) * 1.02
    values[:6, 0] = np.array([0, 1, .5 / 255, 127.5 / 255, 254.5 / 255, 1.01], np.float32)
    # The float texture the baker used to keep in full.
    colors = np.zeros((size * size, 6), dtype=np.float32)
    colors[indices, :channels] = values
    if channels < 6:
        colors[indices, 5] = 1
    nearest = distance_transform_edt(~mask, return_distances=False, return_indices=True)
    filled = colors.reshape(size, size, 6)[nearest[0], nearest[1]]
    expected_color = (filled[..., [0, 1, 2, 5]] * 255).round().astype(np.uint8)
    expected_mr = np.zeros((size, size, 3), dtype=np.uint8)
    expected_mr[..., 1], expected_mr[..., 2] = (filled[..., 4] * 255).round(), (filled[..., 3] * 255).round()
    # Quantized chunk by chunk, then padded in place.
    base_color, mr = np.zeros((size, size, 4), np.uint8), np.zeros((size, size, 3), np.uint8)
    flat_color, flat_mr = base_color.reshape(-1, 4), mr.reshape(-1, 3)
    for start in range(0, len(indices), 1000):
        part = indices[start:start + 1000]
        flat_color[part], flat_mr[part, 1], flat_mr[part, 2] = quantize(values[start:start + 1000])
    pad((base_color, mr), mask, rows=7)
    assert base_color.tobytes() == expected_color.tobytes()
    assert mr.tobytes() == expected_mr.tobytes()


def test_sequential_npz_reads_back_like_savez(tmp_path):
    from local_assets_engine.workers.trellis_infer import save_npz

    arrays = {"vertices": np.random.default_rng(0).random((50, 3), dtype=np.float32),
              "faces": np.arange(30, dtype=np.int64).reshape(10, 3), "voxel_size": 1 / 512}
    save_npz(tmp_path / "a.npz", [(name, lambda value=value: value) for name, value in arrays.items()])
    np.savez(tmp_path / "b.npz", **arrays)
    with np.load(tmp_path / "a.npz", allow_pickle=False) as a, np.load(tmp_path / "b.npz", allow_pickle=False) as b:
        assert sorted(a.files) == sorted(b.files)
        for name in b.files:
            assert a[name].dtype == b[name].dtype and np.array_equal(a[name], b[name])
