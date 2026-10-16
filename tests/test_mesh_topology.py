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


def test_parts_flag_overlap_inside_out_and_floating_but_not_hidden_inner_walls():
    import trimesh

    def check(mesh):
        return topology_checks(np.asarray(mesh.vertices), np.asarray(mesh.faces))

    a, b = trimesh.creation.box(), trimesh.creation.box()
    b.apply_translation([0.5, 0.5, 0.5])
    assert check(trimesh.util.concatenate([a, b]))["intersectingParts"] == 1
    flipped = trimesh.creation.box()
    flipped.invert()
    assert check(flipped)["inwardParts"] == 1
    body, chip = trimesh.creation.box(), trimesh.creation.box(extents=[0.1] * 3)
    chip.apply_translation([0, 0, 2])
    assert check(trimesh.util.concatenate([body, chip]))["floatingParts"] == 1
    outer, inner = trimesh.creation.box(extents=[1] * 3), trimesh.creation.box(extents=[0.9] * 3)
    inner.invert()
    hollow = check(trimesh.util.concatenate([outer, inner]))
    # 속이 빈 물체의 안쪽 벽은 숨은 면이지 잘못이 아니다. 비율만 남기고 경고하지 않는다.
    assert hollow["innerShells"] == 1 and hollow["innerShellFaceShare"] == 0.5 and hollow["warnings"] == []
    assert check(trimesh.creation.box())["warnings"] == []
