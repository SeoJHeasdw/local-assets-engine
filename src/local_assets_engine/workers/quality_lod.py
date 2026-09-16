"""Derive a geometric LOD before UV baking, with a strict surface-error limit."""

import argparse
import json
from pathlib import Path
import subprocess

import numpy as np


def run(master, output, target_faces, gltfpack):
    import trimesh
    from quality_surface import clean_mesh, topology

    data = np.load(master, allow_pickle=False)
    vertices, faces = data["vertices"], data["faces"]
    work = output.parent / "lod-work"
    work.mkdir(parents=True, exist_ok=True)
    original, simplified = work / "master.glb", work / "game.glb"
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    # Normals guide the simplifier around material-independent sharp features.
    mesh.vertex_normals
    mesh.export(original)
    baseline = topology(vertices, faces)
    attempts = []
    selected_error = 0.0
    if gltfpack and target_faces < len(faces):
        # A target count is a budget, not permission to damage the surface.
        # Reject topology regressions and progressively tighten the error bound.
        for error in (0.0005, 0.00025, 0.0001, 0.00005):
            subprocess.run([str(gltfpack), "-i", str(original), "-o", str(simplified),
                            "-si", str(target_faces / len(faces)), "-se", str(error), "-slb", "-noq"], check=True)
            mesh = trimesh.load_scene(simplified, process=False).to_mesh()
            candidate_v, candidate_f = clean_mesh(mesh.vertices, mesh.faces)
            checks = topology(candidate_v, candidate_f)
            attempts.append({"maxRelativeError": error, "triangles": len(candidate_f), "topology": checks})
            if all(checks[key] <= baseline[key] for key in baseline):
                vertices, faces, selected_error = candidate_v, candidate_f, error
                break
    np.savez(output, vertices=vertices, faces=faces)
    report = {"targetFaces": target_faces, "vertices": len(vertices), "triangles": len(faces),
              "topology": topology(vertices, faces), "maxRelativeError": selected_error,
              "method": "meshoptimizer-error-bounded" if selected_error else "preserved-master",
              "attempts": attempts, "warnings": []}
    if len(faces) > target_faces:
        report["warnings"].append("형상 보존을 위해 게임용 목표 면 수를 초과했습니다.")
    output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-faces", type=int, required=True)
    parser.add_argument("--gltfpack", type=Path)
    args = parser.parse_args()
    run(args.master, args.output, args.target_faces, args.gltfpack)
