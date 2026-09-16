"""GLB contract for trellis-mac's KDTree path (runs without the engine imports).

The baker writes image rows in increasing V. trimesh flips V when exporting,
so compensate once here. TRELLIS uses Z-up; glTF uses Y-up. Its unremeshed
surfaces also need the official exporter's double-sided material contract.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

EXPORT_MARKER = "local_assets_export"
EXPORT_VERSION = 1


def export_glb_with_texture(vertices, faces, uvs, base_color_img, mr_img=None,
                            output_path="output.glb"):
    import trimesh
    from PIL import Image

    # Never mutate arrays shared with the baker or the original OBJ export.
    source_positions = np.asarray(vertices)
    positions = source_positions.copy()
    positions[:, 1], positions[:, 2] = source_positions[:, 2], -source_positions[:, 1]
    texture_uv = np.asarray(uvs).copy()
    texture_uv[:, 1] = 1 - texture_uv[:, 1]
    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.fromarray(np.asarray(base_color_img)),
        metallicRoughnessTexture=Image.fromarray(np.asarray(mr_img)) if mr_img is not None else None,
        metallicFactor=0.0, roughnessFactor=0.8, doubleSided=True,
    )
    mesh = trimesh.Trimesh(
        vertices=positions, faces=faces, process=False,
        visual=trimesh.visual.TextureVisuals(uv=texture_uv, material=material),
        metadata={EXPORT_MARKER: {"version": EXPORT_VERSION, "backend": "kdtree"}},
    )
    mesh.export(str(output_path))
    return output_path


def repair_legacy_glb(source: Path, output: Path) -> None:
    """Re-export a known legacy KDTree raw file, keeping its baked images.

    The recipe must establish provenance from the source job's log. This is
    intentionally not an automatic correction for arbitrary glTF files.
    """
    import trimesh

    if source.resolve() == output.resolve():
        raise ValueError("원본 GLB를 덮어쓸 수 없습니다.")
    scene = trimesh.load_scene(source, process=False)
    if len(scene.geometry) != 1 or len(scene.graph.nodes_geometry) != 1:
        raise ValueError("이전 KDTree 단일 메시 GLB만 복구할 수 있습니다.")
    transform, name = scene.graph[scene.graph.nodes_geometry[0]]
    if not np.allclose(transform, np.eye(4)):
        raise ValueError("변환이 적용된 GLB는 이전 KDTree 원본이 아닙니다.")
    mesh = scene.geometry[name]
    if EXPORT_MARKER in mesh.metadata:
        raise ValueError("이미 내보내기 보정이 적용된 GLB입니다.")
    if mesh.visual.kind != "texture":
        raise ValueError("복구할 기본색 텍스처와 UV가 없습니다.")
    material = mesh.visual.material
    if getattr(material, "baseColorTexture", None) is None:
        raise ValueError("복구할 기본색 텍스처와 UV가 없습니다.")
    output.parent.mkdir(parents=True, exist_ok=True)
    export_glb_with_texture(
        mesh.vertices, mesh.faces, mesh.visual.uv,
        np.asarray(material.baseColorTexture),
        np.asarray(material.metallicRoughnessTexture) if material.metallicRoughnessTexture else None,
        output,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="기존 KDTree raw GLB 내보내기 보정")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repair_legacy_glb(args.input, args.output)
