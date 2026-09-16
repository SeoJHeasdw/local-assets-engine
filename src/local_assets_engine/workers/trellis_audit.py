"""Opt-in timing and pre-decimation snapshots; does not change model settings.

Executed inside the measured TRELLIS process. Nested timings must not be added
together. The snapshots allow postprocessing comparisons without more inference.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
import runpy
import time


def run(script, directory):
    import numpy as np
    import torch

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    record = {"timings": [], "settings": {}, "state": "running"}

    def write():
        temporary = root / "timings.json.tmp"
        temporary.write_text(json.dumps(record, indent=2), "utf-8")
        temporary.replace(root / "timings.json")

    def sync():
        if torch.backends.mps.is_available():
            torch.mps.synchronize()

    def timed(name, fn, capture=None):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            sync()
            started = time.perf_counter()
            result = fn(*args, **kwargs)
            sync()
            elapsed = time.perf_counter() - started
            record["timings"].append({"name": name, "seconds": round(elapsed, 4)})
            write()
            print(f"[audit] {name}: {elapsed:.3f}s", flush=True)
            if capture:
                saved = time.perf_counter()
                capture(result, args, kwargs)
                record["timings"].append({"name": f"save:{name}", "seconds": round(time.perf_counter() - saved, 4)})
                write()
            return result
        return wrapped

    # Execute generate.py's environment/path setup without invoking its main.
    namespace = runpy.run_path(script, run_name="__trellis_audit__")
    from trellis2.pipelines.trellis2_image_to_3d import Trellis2ImageTo3DPipeline as Pipeline
    from trellis2.models.sc_vaes import fdg_vae
    from backends import texture_baker
    import fast_simplification

    def array(value):
        if hasattr(value, "detach"):
            value = value.detach().cpu()
            if value.dtype == torch.bfloat16:
                value = value.float()
            return value.numpy()
        return np.asarray(value)

    def capture_load(pipeline, _args, _kwargs):
        record["settings"] = {
            "sparseStructure": pipeline.sparse_structure_sampler_params,
            "shape": pipeline.shape_slat_sampler_params,
            "texture": pipeline.tex_slat_sampler_params,
            "lowVram": pipeline.low_vram,
            "torch": torch.__version__,
        }
        write()

    def capture_extraction(_result, args, _kwargs):
        np.savez(root / "dual-grid.npz", coords=array(args[0]), dual_vertices=array(args[1]),
                 intersected=array(args[2]), split_weight=array(args[3]))

    def capture_decoded(result, _args, _kwargs):
        mesh = result[0]
        np.savez(root / "decoded.npz", vertices=array(mesh.vertices), faces=array(mesh.faces),
                 coords=array(mesh.coords), attrs=array(mesh.attrs),
                 origin=array(mesh.origin), voxel_size=array(mesh.voxel_size))

    def capture_simplified(result, _args, _kwargs):
        np.savez(root / "simplified.npz", vertices=result[0], faces=result[1])

    Pipeline.from_pretrained = staticmethod(timed("load_pipeline", Pipeline.from_pretrained, capture_load))
    for name in ("preprocess_image", "get_cond", "sample_sparse_structure", "sample_shape_slat",
                 "sample_tex_slat", "decode_shape_slat", "decode_tex_slat"):
        setattr(Pipeline, name, timed(name, getattr(Pipeline, name)))
    Pipeline.decode_latent = timed("decode_latent", Pipeline.decode_latent, capture_decoded)
    fdg_vae.flexible_dual_grid_to_mesh = timed(
        "extract_mesh", fdg_vae.flexible_dual_grid_to_mesh, capture_extraction,
    )
    fast_simplification.simplify = timed("simplify_to_200k", fast_simplification.simplify, capture_simplified)
    for name in ("uv_unwrap", "bake_texture", "export_glb_with_texture"):
        setattr(texture_baker, name, timed(name, getattr(texture_baker, name)))
    try:
        namespace["main"]()
        record["state"] = "done"
    except BaseException:
        record["state"] = "failed"
        raise
    finally:
        write()
