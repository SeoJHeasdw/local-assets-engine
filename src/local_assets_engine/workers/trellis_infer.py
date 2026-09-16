"""Generate and retain the full model state; no destructive mesh export here."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import runpy
import time


def run(script, arguments, output, model_revision=None):
    # Only the upstream module's setup is needed; its main reduces the mesh
    # before it writes GLB. Surface conversion runs in another measured process.
    runpy.run_path(script, run_name="__local_assets_setup__")
    import numpy as np
    import torch
    from PIL import Image
    from huggingface_hub import hf_hub_download
    from trellis2.pipelines.trellis2_image_to_3d import Trellis2ImageTo3DPipeline as Pipeline
    from trellis2.models.sc_vaes import fdg_vae
    import mesh_extract

    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--pipeline-type", required=True)
    parser.add_argument("--output")  # old CLI compatibility, output is an NPZ now
    parser.add_argument("--texture-size", type=int)
    args = parser.parse_args(arguments)
    mesh_extract.patch(fdg_vae)
    if args.pipeline_type == "512":
        Pipeline.model_names_to_load = [name for name in Pipeline.model_names_to_load if "1024" not in name]
    elif args.pipeline_type == "1024":
        Pipeline.model_names_to_load = [name for name in Pipeline.model_names_to_load if "512" not in name]
    print("Loading pipeline...", flush=True)
    started = time.perf_counter()
    config = hf_hub_download("microsoft/TRELLIS.2-4B", "pipeline.json", revision=model_revision)
    model_dir = str(Path(config).parent)
    pipeline = Pipeline.from_pretrained(model_dir)
    load_seconds = time.perf_counter() - started
    print(f"Loaded in {load_seconds:.1f}s", flush=True)
    pipeline.to(torch.device("mps"))
    print(f"Generating 3D model (pipeline={args.pipeline_type}, seed={args.seed})...", flush=True)
    started = time.perf_counter()
    with Image.open(args.image) as image:
        result = pipeline.run(image, seed=args.seed, pipeline_type=args.pipeline_type)[0]
    torch.mps.synchronize()
    generate_seconds = time.perf_counter() - started
    if not result.faces.numel():
        raise RuntimeError("TRELLIS가 빈 메시를 만들었습니다. GPU 감시 오류를 확인하세요.")
    print(f"Mesh: {len(result.vertices):,} vertices, {len(result.faces):,} triangles", flush=True)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".npz.tmp")
    with temporary.open("wb") as handle:
        np.savez(handle, vertices=result.vertices.float().cpu().numpy(), faces=result.faces.cpu().numpy(),
                 coords=result.coords.cpu().numpy(), attrs=result.attrs.float().cpu().numpy(),
                 origin=result.origin.cpu().numpy(), voxel_size=float(result.voxel_size))
    temporary.replace(target)
    metadata = {
        "version": 1, "model": "microsoft/TRELLIS.2-4B", "revision": Path(config).parent.name,
        "seed": args.seed, "pipelineType": args.pipeline_type,
        "inputSha256": hashlib.sha256(args.image.read_bytes()).hexdigest(),
        "vertices": len(result.vertices), "triangles": len(result.faces),
        "loadSeconds": load_seconds, "generateSeconds": generate_seconds,
        "sampling": {"structure": pipeline.sparse_structure_sampler_params,
                     "shape": pipeline.shape_slat_sampler_params, "texture": pipeline.tex_slat_sampler_params},
        "torch": torch.__version__,
    }
    target.with_suffix(".json").write_text(json.dumps(metadata, indent=2), "utf-8")
    print(f"Saved: {target}", flush=True)
