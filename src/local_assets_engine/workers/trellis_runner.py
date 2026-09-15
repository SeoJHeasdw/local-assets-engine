"""Run trellis-mac's generate.py with commercial-safe background removal.

This file is executed by the TRELLIS virtual environment (Python 3.11), not the
engine's, so it must not import ``local_assets_engine``.

TRELLIS.2's pipeline.json loads ``briaai/RMBG-2.0`` while the pipeline loads.
That model is gated and non-commercial. The load is redirected to BiRefNet
(MIT) before generate.py runs. Inputs that already have alpha skip removal.

Usage: python trellis_runner.py --generate-py PATH [--birefnet REPO]
       [--birefnet-revision SHA] -- IMAGE [generate.py options]
"""

import argparse
import os
import runpy
import sys

RMBG_REPO = "briaai/RMBG-2.0"


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--generate-py", required=True)
    parser.add_argument("--birefnet", default="ZhengPeng7/BiRefNet")
    parser.add_argument("--birefnet-revision", default=None)
    args, rest = parser.parse_known_args()
    if rest[:1] == ["--"]:
        rest = rest[1:]

    script = os.path.abspath(args.generate_py)
    # generate.py sets these before torch is imported; transformers imports torch here first.
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    os.environ.setdefault("ATTN_BACKEND", "sdpa")
    os.environ.setdefault("SPARSE_ATTN_BACKEND", "sdpa")
    # generate.py imports its sibling `backends` package relative to its own folder.
    sys.path.insert(0, os.path.dirname(script))

    from transformers import AutoModelForImageSegmentation

    original = AutoModelForImageSegmentation.from_pretrained

    def redirected(name, *positional, **keywords):
        if str(name) == RMBG_REPO:
            name = args.birefnet
            if args.birefnet_revision:
                keywords["revision"] = args.birefnet_revision
            print(f"[local-assets] background model -> {name}", flush=True)
        return original(name, *positional, **keywords)

    AutoModelForImageSegmentation.from_pretrained = redirected
    sys.argv = [script, *rest]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
