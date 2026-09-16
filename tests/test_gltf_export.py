import json
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image

from local_assets_engine.workers.gltf_export import (
    EXPORT_MARKER, export_glb_with_texture, repair_legacy_glb,
)


def read_glb(path):
    data = path.read_bytes()
    length = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20:20 + length]), data[28 + length:]


def attribute(document, binary, name):
    primitive = document["meshes"][0]["primitives"][0]
    accessor = document["accessors"][primitive["attributes"][name]]
    view = document["bufferViews"][accessor["bufferView"]]
    width = {"VEC2": 2, "VEC3": 3}[accessor["type"]]
    return np.ndarray(
        (accessor["count"], width), dtype="<f4", buffer=binary,
        offset=view.get("byteOffset", 0) + accessor.get("byteOffset", 0),
        strides=(view.get("byteStride", width * 4), 4),
    )


@pytest.fixture
def surface():
    return (
        np.array([[0., 1., 0.], [2., 1., 0.], [0., 1., 3.]]),
        np.array([[0, 1, 2]]),
        np.array([[0.2, 0.2], [0.8, 0.2], [0.2, 0.8]]),
        np.array([[[255, 0, 0], [0, 255, 0]], [[0, 0, 255], [255, 255, 0]]], dtype=np.uint8),
    )


def test_export_preserves_baked_row_coordinates_and_converts_z_up(tmp_path, surface):
    vertices, faces, uv, texture = surface
    original_vertices, original_uv = vertices.copy(), uv.copy()
    path = tmp_path / "correct.glb"
    export_glb_with_texture(vertices, faces, uv, texture, output_path=path)
    doc, binary = read_glb(path)
    # The baker's V=0.2 sampled image row 0 (red/green), not row 1 (blue/yellow).
    # This must remain V=0.2 in the glTF file, despite trimesh's V flip.
    exported_uv = attribute(doc, binary, "TEXCOORD_0")
    np.testing.assert_allclose(exported_uv, uv)
    np.testing.assert_allclose(attribute(doc, binary, "POSITION"), [[0, 0, -1], [2, 0, -1], [0, 3, -1]])
    assert doc["materials"][0]["doubleSided"] is True
    assert doc["meshes"][0]["extras"][EXPORT_MARKER]["version"] == 1
    np.testing.assert_array_equal(vertices, original_vertices)
    np.testing.assert_array_equal(uv, original_uv)


def legacy_mesh(surface):
    vertices, faces, uv, texture = surface
    return trimesh.Trimesh(
        vertices=vertices, faces=faces, process=False,
        visual=trimesh.visual.TextureVisuals(
            uv=uv, material=trimesh.visual.material.PBRMaterial(
                baseColorTexture=Image.fromarray(texture), metallicFactor=0, roughnessFactor=0.8,
            ),
        ),
    )


def test_legacy_recovery_matches_new_export_and_does_not_overwrite(tmp_path, surface):
    source, repaired = tmp_path / "old.glb", tmp_path / "repaired.glb"
    legacy_mesh(surface).export(source)
    original = source.read_bytes()
    repair_legacy_glb(source, repaired)
    doc, binary = read_glb(repaired)
    np.testing.assert_allclose(attribute(doc, binary, "TEXCOORD_0"), surface[2])
    np.testing.assert_allclose(attribute(doc, binary, "POSITION"), [[0, 0, -1], [2, 0, -1], [0, 3, -1]])
    assert doc["materials"][0]["doubleSided"] is True
    loaded = next(iter(trimesh.load_scene(repaired, process=False).geometry.values()))
    np.testing.assert_array_equal(np.asarray(loaded.visual.material.baseColorTexture), surface[3])
    assert source.read_bytes() == original
    with pytest.raises(ValueError, match="이미"):
        repair_legacy_glb(repaired, tmp_path / "twice.glb")
    with pytest.raises(ValueError, match="덮어"):
        repair_legacy_glb(source, source)


def test_recovery_refuses_transformed_or_multiple_meshes(tmp_path, surface):
    mesh = legacy_mesh(surface)
    scene = trimesh.Scene()
    transform = np.eye(4)
    transform[0, 3] = 2
    scene.add_geometry(mesh, transform=transform)
    source = tmp_path / "scene.glb"
    scene.export(source)
    with pytest.raises(ValueError, match="변환"):
        repair_legacy_glb(source, tmp_path / "out.glb")
    scene.add_geometry(mesh.copy())
    scene.export(source)
    with pytest.raises(ValueError, match="단일"):
        repair_legacy_glb(source, tmp_path / "out.glb")


def test_generation_runner_actually_installs_the_export_fix(tmp_path):
    # Exercise the generate.py interception without loading model weights.
    (tmp_path / "transformers.py").write_text(
        "class AutoModelForImageSegmentation:\n"
        "    @staticmethod\n"
        "    def from_pretrained(*a, **kw): raise AssertionError('no model loading')\n"
    )
    (tmp_path / "backends").mkdir()
    (tmp_path / "backends/__init__.py").touch()
    (tmp_path / "backends/texture_baker.py").write_text(
        "def _rasterize_uv_triangles(*a): pass\n"
        "def export_glb_with_texture(*a, **kw): raise AssertionError('unpatched export')\n"
    )
    script = tmp_path / "generate.py"
    script.write_text(
        "import numpy as np\n"
        "from backends.texture_baker import export_glb_with_texture\n"
        "export_glb_with_texture(np.array([[0.,0.,0.],[1.,0.,0.],[0.,0.,2.]]),\n"
        "    np.array([[0,1,2]]),np.array([[0.,.25],[1.,.25],[0.,.75]]),\n"
        "    np.full((2,2,3),255,dtype=np.uint8),output_path='generated.glb')\n"
    )
    worker = Path(__file__).resolve().parents[1] / "src/local_assets_engine/workers/trellis_runner.py"
    result = subprocess.run(
        [sys.executable, str(worker), "--generate-py", str(script)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    doc, binary = read_glb(tmp_path / "generated.glb")
    assert doc["materials"][0]["doubleSided"] is True
    np.testing.assert_allclose(attribute(doc, binary, "POSITION")[:, 1], [0, 0, 2])
    np.testing.assert_allclose(attribute(doc, binary, "TEXCOORD_0")[:, 1], [.25, .25, .75])


def test_native_pbr_keeps_factors_and_rotates_smooth_normals(tmp_path, surface):
    vertices, faces, uv, texture = surface
    normals = np.tile([0., 0., 1.], (3, 1))
    path = tmp_path / "native.glb"
    export_glb_with_texture(vertices, faces, uv, texture, texture, path,
                            normals=normals, native_pbr=True)
    doc, binary = read_glb(path)
    pbr = doc["materials"][0]["pbrMetallicRoughness"]
    assert pbr["metallicFactor"] == 1 and pbr["roughnessFactor"] == 1
    np.testing.assert_allclose(attribute(doc, binary, "NORMAL"), [[0, 1, 0]] * 3)
    assert doc["meshes"][0]["extras"][EXPORT_MARKER]["version"] == 2
