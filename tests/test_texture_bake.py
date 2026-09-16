import numpy as np

from local_assets_engine.workers.texture_bake import fill_holes, sample_voxels


def test_far_voxels_do_not_colour_a_texel():
    distances = np.array([[0.1, 0.2], [5.0, 6.0]])
    indices = np.array([[0, 1], [0, 1]])
    attrs = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    sampled, has_neighbor = sample_voxels(distances, indices, attrs, voxel_size=0.1)
    assert has_neighbor.tolist() == [True, False]
    # 가까운 복셀이 뚜렷하게 앞선다. 섞이면 재질 경계에서 색이 탈색된다.
    assert sampled[0][0] > 0.7 > sampled[0][1]
    assert sampled[1].tolist() == [0.0, 0.0, 0.0]


def test_holes_take_the_colour_of_the_nearest_filled_texel():
    image = np.zeros((4, 4, 3), dtype=np.float32)
    valid = np.zeros((4, 4), dtype=bool)
    image[0, 0] = (0.1, 0.2, 0.3)
    image[3, 3] = (0.9, 0.5, 0.2)
    valid[0, 0] = valid[3, 3] = True
    filled = fill_holes(image, valid)
    assert tuple(filled[0, 1]) == (0.1, 0.2, 0.3)
    assert tuple(filled[3, 2]) == (0.9, 0.5, 0.2)
    assert tuple(filled[0, 0]) == (0.1, 0.2, 0.3)
    # 검게 남는 텍셀이 없다. 예전 방식은 검은색을 이웃에 섞었다.
    assert filled.max(axis=-1).min() > 0


def test_an_empty_or_full_mask_is_left_alone():
    image = np.ones((2, 2, 3), dtype=np.float32)
    assert fill_holes(image, np.zeros((2, 2), dtype=bool)) is image
    assert fill_holes(image, np.ones((2, 2), dtype=bool)) is image
