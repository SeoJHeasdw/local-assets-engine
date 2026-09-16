import numpy as np

from local_assets_engine.tools.blender_post import topology_checks


def test_uv_seams_are_not_reported_as_holes():
    vertices = np.array([[0., 0, 0], [1., 0, 0], [0., 1, 0], [0., 0, 1]])
    faces = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
    # Give every face its own vertices, like an atlas with a seam at each edge.
    split = vertices[faces].reshape(-1, 3)
    split_faces = np.arange(len(split)).reshape(-1, 3)
    assert topology_checks(split, split_faces)["warnings"] == []
    split_faces[0] = split_faces[0, ::-1]
    assert topology_checks(split, split_faces)["inconsistentWindingEdges"] == 3


def test_checks_distinguish_open_surface_and_duplicate_triangles_without_mutation():
    vertices = np.array([[0., 0, 0], [1., 0, 0], [0., 1, 0]])
    faces = np.array([[0, 1, 2]])
    result = topology_checks(vertices, faces)
    assert result["boundaryEdges"] == 3
    assert result["nonManifoldEdges"] == 0
    result = topology_checks(vertices, np.repeat(faces, 3, axis=0))
    assert result["duplicateFaces"] == 2
    assert result["nonManifoldEdges"] == 3
    np.testing.assert_array_equal(faces, [[0, 1, 2]])
