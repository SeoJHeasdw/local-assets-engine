"""Generate and retain the full model state; no destructive mesh export here."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import runpy
import time


def save_npz(path, items):
    """Write NPZ entries one at a time, each array alive on the CPU only while it is written.

    Same layout as ``np.savez`` (stored ZIP of ``<name>.npy``), so ``np.load`` reads it unchanged.
    ``items`` are ``(name, produce)`` pairs; ``produce`` makes the array when its turn comes.
    """
    import zipfile

    import numpy as np
    from numpy.lib import format as npy

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name, produce in items:
            array = np.asanyarray(produce())
            with archive.open(f"{name}.npy", "w", force_zip64=True) as entry:
                npy.write_array(entry, array, allow_pickle=False)
            del array


def models_for(names, pipeline_type):
    """Only the models the pipeline type samples with; the rest would sit in memory unused.

    The pipeline seeds torch right before sampling, so loading fewer models leaves the result
    of a seed unchanged.
    """
    if pipeline_type == "512":
        return [name for name in names if "1024" not in name]
    if pipeline_type == "1024":
        return [name for name in names if "512" not in name]
    if pipeline_type == "1024_cascade":
        # Shape is sampled at 512 then 1024; texture only at 1024.
        return [name for name in names if name != "tex_slat_flow_model_512"]
    return list(names)


def run(script, arguments, output, model_revision=None, model_provenance=None, configure_models=None):
    # Only the upstream module's setup is needed; its main reduces the mesh
    # before it writes GLB. Surface conversion runs in another measured process.
    runpy.run_path(script, run_name="__local_assets_setup__")
    import numpy as np
    import torch
    from PIL import Image
    from huggingface_hub import hf_hub_download
    from trellis2.pipelines.trellis2_image_to_3d import Trellis2ImageTo3DPipeline as Pipeline
    from trellis2.models.sc_vaes import fdg_vae
    from trellis2.modules.sparse.conv import conv_none
    import mesh_extract
    import sparse_neighbors

    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--pipeline-type", required=True)
    parser.add_argument("--output")  # old CLI compatibility, output is an NPZ now
    parser.add_argument("--texture-size", type=int)
    args = parser.parse_args(arguments)
    mesh_extract.patch(fdg_vae)
    sparse_neighbors.patch(conv_none)
    Pipeline.model_names_to_load = models_for(Pipeline.model_names_to_load, args.pipeline_type)
    print("Loading pipeline...", flush=True)
    started = time.perf_counter()
    config = hf_hub_download("microsoft/TRELLIS.2-4B", "pipeline.json", revision=model_revision)
    model_dir = str(Path(config).parent)
    if configure_models:
        configure_models(model_dir)
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
    sampling = {"structure": pipeline.sparse_structure_sampler_params,
                "shape": pipeline.shape_slat_sampler_params, "texture": pipeline.tex_slat_sampler_params}
    # The models are done; release them before the result is copied to the CPU.
    del pipeline
    torch.mps.empty_cache()
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".npz.tmp")
    save_npz(temporary, [
        ("vertices", lambda: result.vertices.float().cpu().numpy()), ("faces", lambda: result.faces.cpu().numpy()),
        ("coords", lambda: result.coords.cpu().numpy()), ("attrs", lambda: result.attrs.float().cpu().numpy()),
        ("origin", lambda: result.origin.cpu().numpy()), ("voxel_size", lambda: float(result.voxel_size)),
    ])
    temporary.replace(target)
    metadata = {
        "version": 1, "model": "microsoft/TRELLIS.2-4B", "revision": Path(config).parent.name,
        "seed": args.seed, "pipelineType": args.pipeline_type,
        "inputSha256": hashlib.sha256(args.image.read_bytes()).hexdigest(),
        "vertices": len(result.vertices), "triangles": len(result.faces),
        "loadSeconds": load_seconds, "generateSeconds": generate_seconds,
        "sampling": sampling,
        "torch": torch.__version__,
        "models": model_provenance or {},
    }
    target.with_suffix(".json").write_text(json.dumps(metadata, indent=2), "utf-8")
    print(f"Saved: {target}", flush=True)
