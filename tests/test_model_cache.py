import json
import struct
from pathlib import Path

import pytest

from local_assets_engine.workers.model_cache import valid_safetensors, weight_files_missing
from local_assets_engine.workers.trellis_runner import install_model_pins, bind_checkpoint_loader
from local_assets_engine.presets import PresetError, load_presets


def tiny_weights(path: Path):
    header = json.dumps({"a": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    path.write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * 4)


def test_complete_header_and_payload_are_required_without_loading_tensors(tmp_path):
    path = tmp_path / "model.safetensors"
    tiny_weights(path)
    assert valid_safetensors(path)
    complete = path.read_bytes()
    for broken in (b"", b"garbage", complete[:-1], complete + b"trailing"):
        path.write_bytes(broken)
        assert not valid_safetensors(path)
    assert weight_files_missing(tmp_path) == [path.name]


def test_bad_shard_indexes_are_reported_instead_of_throwing(tmp_path):
    index = tmp_path / "model.safetensors.index.json"
    nested = '{"a":' * 2000 + '0' + '}' * 2000
    for contents in ("{", "{}", nested, json.dumps({"weight_map": {"a": "../outside.safetensors"}})):
        index.write_text(contents)
        assert weight_files_missing(tmp_path)


def test_bad_safetensors_metadata_is_not_ready(tmp_path):
    path = tmp_path / "bad.safetensors"
    for metadata in ([1], {"info": 4}):
        header = json.dumps({"__metadata__": metadata, "a": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
        path.write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * 4)
        assert not valid_safetensors(path)
    nested = ('{"a":' * 2000 + '0' + '}' * 2000).encode()
    path.write_bytes(struct.pack("<Q", len(nested)) + nested)
    assert not valid_safetensors(path)


def test_doctor_requires_the_pinned_pipeline_and_declared_checkpoints(tmp_path, monkeypatch):
    from local_assets_engine.doctor import is_cached
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    repo, revision = "microsoft/TRELLIS.2-4B", "a" * 40
    snapshot = tmp_path / "models--microsoft--TRELLIS.2-4B/snapshots" / revision
    folder = snapshot / "ckpts"
    folder.mkdir(parents=True)
    (folder / "partial.json").write_text("{}")
    tiny_weights(folder / "partial.safetensors")
    assert not is_cached(repo, revision)
    pipeline = snapshot / "pipeline.json"
    pipeline.write_text(json.dumps({"args": {"models": ["wrong-type"]}}))
    assert not is_cached(repo, revision)
    pipeline.write_text(json.dumps({"args": {"models": {"needed": "ckpts/required"}}}))
    assert not is_cached(repo, revision)
    (folder / "required.json").write_text("{}")
    tiny_weights(folder / "required.safetensors")
    assert is_cached(repo, revision)


def test_upstream_download_and_snapshot_entrypoints_are_pinned_and_offline(tmp_path, monkeypatch):
    import huggingface_hub as hub
    from huggingface_hub import file_download
    import huggingface_hub._snapshot_download as snapshot_module

    calls = []
    path = tmp_path / ("c" * 64)
    path.write_bytes(b"small inert cached file")
    def download(repo, name, **kwargs):
        calls.append((repo, name, kwargs))
        return str(path)
    def snapshot(repo, **kwargs):
        calls.append((repo, None, kwargs))
        return str(tmp_path)
    monkeypatch.setattr(hub, "hf_hub_download", download)
    monkeypatch.setattr(hub, "snapshot_download", snapshot)
    # Register restoration for direct module entrypoints mutated by the wrapper.
    monkeypatch.setattr(file_download, "hf_hub_download", file_download.hf_hub_download)
    monkeypatch.setattr(snapshot_module, "hf_hub_download", snapshot_module.hf_hub_download)
    monkeypatch.setattr(snapshot_module, "snapshot_download", snapshot_module.snapshot_download)
    pins = {"owner/pipeline": "a" * 40, "owner/decoder": "b" * 40}
    provenance = install_model_pins(pins, hub)
    hub.hf_hub_download("owner/pipeline", "pipeline.json")
    file_download.hf_hub_download("owner/decoder", "weights.safetensors")
    hub.snapshot_download("owner/decoder")
    snapshot_module.snapshot_download("owner/decoder")
    assert all(call[2] == {"revision": pins[call[0]], "local_files_only": True} for call in calls)
    assert provenance["owner/decoder"]["files"]["weights.safetensors"]["cacheBlobSha256"] == "c" * 64
    with pytest.raises(ValueError, match="커밋"):
        hub.hf_hub_download("owner/decoder", "weights", revision="main")
    with pytest.raises(ValueError, match="고정되지"):
        hub.hf_hub_download("unapproved/repository", "weights")
    assert len(calls) == 4


def test_remote_code_and_transitive_model_pins_cannot_float(tmp_path):
    presets = load_presets()
    path = tmp_path / "presets.json"
    presets["backgroundRemoval"]["revision"] = "main"
    path.write_text(json.dumps(presets))
    with pytest.raises(PresetError, match="backgroundRemoval"):
        load_presets(path)
    presets = load_presets()
    presets["mesh"]["dependencies"]["facebook/dinov3-vitl16-pretrain-lvd1689m"] = "main"
    path.write_text(json.dumps(presets))
    with pytest.raises(PresetError, match="DINOv3"):
        load_presets(path)


def test_local_checkpoint_provenance_and_original_failure_are_preserved(tmp_path):
    from types import SimpleNamespace
    folder = tmp_path / "ckpts"
    folder.mkdir()
    (folder / "flow.json").write_text("{}")
    tiny_weights(folder / "flow.safetensors")
    calls = []
    failure = ValueError("injected incompatible tensor")
    def original(path, **kwargs):
        calls.append(path)
        raise failure
    models = SimpleNamespace(from_pretrained=original)
    provenance = {"owner/main": {"revision": "a" * 40, "files": {}},
                  "owner/external": {"revision": "b" * 40, "files": {}}}
    bind_checkpoint_loader(models, tmp_path, "owner/main", provenance)
    with pytest.raises(ValueError) as first:
        models.from_pretrained(str(folder / "flow"))
    with pytest.raises(ValueError) as second:
        models.from_pretrained("ckpts/flow")
    assert first.value is failure and second.value is failure
    assert calls == [str(folder / "flow")]
    assert set(provenance["owner/main"]["files"]) == {"ckpts/flow.json", "ckpts/flow.safetensors"}
    # External prefix is normalized before the original loader resolves HF files.
    with pytest.raises(ValueError):
        models.from_pretrained(str(tmp_path / "owner/external/ckpts/decoder"))
    assert calls[-1] == "owner/external/ckpts/decoder"
