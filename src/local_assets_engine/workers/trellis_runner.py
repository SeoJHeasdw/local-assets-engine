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
import json
import re
import hashlib
from pathlib import Path

RMBG_REPO = "briaai/RMBG-2.0"
COMMIT = re.compile(r"^[0-9a-f]{40}$")


def install_model_pins(pins, hub):
    """Bind every upstream HF entrypoint to the same existing local snapshots."""
    if not pins or not all(COMMIT.fullmatch(str(value)) for value in pins.values()):
        raise ValueError("모든 TRELLIS 모델은 40자리 커밋으로 고정해야 합니다.")
    provenance = {repo: {"revision": revision, "files": {}} for repo, revision in pins.items()}
    original_download = hub.hf_hub_download
    original_snapshot = hub.snapshot_download

    def options(repo, keywords):
        if repo not in pins:
            raise ValueError(f"고정되지 않은 모델 저장소입니다: {repo}")
        revision = keywords.get("revision")
        if revision is not None and revision != pins[repo]:
            raise ValueError(f"모델 커밋이 기록된 설정과 다릅니다: {repo}")
        return {**keywords, "revision": pins[repo], "local_files_only": True}

    def download(repo_id, filename, *args, **kwargs):
        result = original_download(repo_id, filename, *args, **options(repo_id, kwargs))
        path = Path(result)
        blob = path.resolve().name
        info = {"bytes": path.stat().st_size}
        if re.fullmatch(r"[0-9a-f]{64}", blob):
            info["cacheBlobSha256"] = blob
        provenance[repo_id]["files"][filename] = info
        return result

    def snapshot(repo_id, *args, **kwargs):
        return original_snapshot(repo_id, *args, **options(repo_id, kwargs))

    hub.hf_hub_download = download
    hub.snapshot_download = snapshot
    # Some upstream loaders import directly from these public implementation
    # modules instead of the package facade. Bind those references as well.
    from huggingface_hub import file_download
    file_download.hf_hub_download = download
    import huggingface_hub._snapshot_download as snapshot_module
    snapshot_module.hf_hub_download = download
    snapshot_module.snapshot_download = snapshot
    return provenance


def bind_checkpoint_loader(models, snapshot, main_repo, provenance):
    """Record local checkpoints and retain failures masked by upstream fallback."""
    snapshot = Path(snapshot)
    original = models.from_pretrained
    failures = {}

    def load(path, **kwargs):
        given = str(path)
        try:
            relative = Path(given).relative_to(snapshot).as_posix()
        except ValueError:
            relative = given
        # The pipeline prefixes the full snapshot path even for external repos.
        # Normalize those before the upstream loader invents an invalid repo ID.
        external = next((repo for repo in provenance if relative.startswith(repo + "/")), None)
        target = relative if external else given
        key = relative
        if key in failures:
            raise failures[key]
        try:
            if external is None:
                if not Path(given).is_absolute() or not given.startswith(str(snapshot) + os.sep):
                    raise ValueError(f"고정되지 않은 checkpoint 경로입니다: {given}")
                for suffix in (".json", ".safetensors"):
                    file = Path(given + suffix)
                    if not file.is_file():
                        raise FileNotFoundError(f"고정 checkpoint 파일이 없습니다: {relative}{suffix}. docs/SETUP.md를 확인하세요.")
                    info = {"bytes": file.stat().st_size}
                    blob = file.resolve().name
                    if re.fullmatch(r"[0-9a-f]{64}", blob):
                        info["cacheBlobSha256"] = blob
                    if suffix == ".json":
                        info["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
                    provenance[main_repo]["files"][relative + suffix] = info
            return original(target, **kwargs)
        except Exception as error:
            failures[key] = error
            raise

    models.from_pretrained = load
    return load


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--generate-py", required=True)
    parser.add_argument("--birefnet", default="ZhengPeng7/BiRefNet")
    parser.add_argument("--birefnet-revision", required=True)
    parser.add_argument("--audit-dir", default=None)
    parser.add_argument("--state-output", default=None)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--model-pins", required=True)
    args, rest = parser.parse_known_args()
    if not COMMIT.fullmatch(args.birefnet_revision) or not COMMIT.fullmatch(args.model_revision):
        parser.error("모델 revision은 40자리 커밋 해시여야 합니다.")
    pins = json.loads(args.model_pins)
    if not isinstance(pins, dict):
        parser.error("model-pins는 저장소별 커밋 객체여야 합니다.")
    pins.update({"microsoft/TRELLIS.2-4B": args.model_revision, args.birefnet: args.birefnet_revision})
    if rest[:1] == ["--"]:
        rest = rest[1:]

    script = os.path.abspath(args.generate_py)
    # generate.py sets these before torch is imported; transformers imports torch here first.
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    os.environ.setdefault("ATTN_BACKEND", "sdpa")
    os.environ.setdefault("SPARSE_ATTN_BACKEND", "sdpa")
    # generate.py imports its sibling `backends` package relative to its own folder.
    sys.path.insert(0, os.path.dirname(script))

    os.environ["HF_HUB_OFFLINE"] = "1"
    import huggingface_hub
    provenance = install_model_pins(pins, huggingface_hub)
    from transformers import AutoModelForImageSegmentation, DINOv3ViTModel

    original = AutoModelForImageSegmentation.from_pretrained

    def redirected(name, *positional, **keywords):
        if str(name) == RMBG_REPO:
            name = args.birefnet
            print(f"[local-assets] background model -> {name}", flush=True)
        if str(name) not in pins:
            raise ValueError(f"고정되지 않은 배경 제거 모델입니다: {name}")
        keywords.update(revision=pins[str(name)], local_files_only=True)
        return original(name, *positional, **keywords)

    AutoModelForImageSegmentation.from_pretrained = redirected
    original_dino = DINOv3ViTModel.from_pretrained
    def pinned_dino(name, *positional, **keywords):
        if str(name) not in pins:
            raise ValueError(f"고정되지 않은 이미지 조건 모델입니다: {name}")
        keywords.update(revision=pins[str(name)], local_files_only=True)
        return original_dino(name, *positional, **keywords)
    DINOv3ViTModel.from_pretrained = pinned_dino
    # 대체 경로만 보정한다. Metal 경로의 UV와 좌표축을 두 번 바꾸면 안 된다.
    # 보정 로드 실패를 무시하면 깨진 GLB를 성공으로 기록하므로 여기서 실패시킨다.
    import texture_bake
    import gltf_export
    from backends import texture_baker

    texture_bake.patch(texture_baker)
    texture_baker.export_glb_with_texture = gltf_export.export_glb_with_texture
    print("[local-assets] Full-resolution source → quality surface/PBR" if args.state_output
          else f"[local-assets] KDTree GLB export v{gltf_export.EXPORT_VERSION}", flush=True)

    sys.argv = [script, *rest]
    if args.state_output:
        import trellis_infer
        def configure_models(snapshot):
            from trellis2 import models
            bind_checkpoint_loader(models, snapshot, "microsoft/TRELLIS.2-4B", provenance)

        trellis_infer.run(script, rest, args.state_output, args.model_revision, provenance, configure_models)
    elif args.audit_dir:
        import trellis_audit

        trellis_audit.run(script, args.audit_dir)
    else:
        runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
