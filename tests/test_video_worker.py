import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from local_assets_engine.workers import video_runner as worker


def fake_snapshot(root: Path, *, skip: str | None = None) -> Path:
    """The file layout of a diffusers Wan snapshot with empty weight files."""
    files = {
        "model_index.json": "{}",
        "scheduler/scheduler_config.json": "{}",
        "tokenizer/tokenizer.json": "{}",
        "text_encoder/config.json": "{}",
        "text_encoder/model.safetensors.index.json": json.dumps({"weight_map": {
            "a": "model-00001-of-00002.safetensors", "b": "model-00002-of-00002.safetensors"}}),
        "text_encoder/model-00001-of-00002.safetensors": "",
        "text_encoder/model-00002-of-00002.safetensors": "",
        "transformer/config.json": "{}",
        "transformer/diffusion_pytorch_model.safetensors.index.json": json.dumps({"weight_map": {
            "a": "diffusion_pytorch_model-00001-of-00001.safetensors"}}),
        "transformer/diffusion_pytorch_model-00001-of-00001.safetensors": "",
        "vae/config.json": "{}",
        "vae/diffusion_pytorch_model.safetensors": "",
    }
    for name, text in files.items():
        if name == skip:
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


class FakeBackend:
    """Stands in for DiffusersBackend: same calls, numpy frames, no model."""

    def __init__(self, snapshot: Path):
        self._snapshot = snapshot
        self.peak = 4_000_000_000
        self.calls: list[tuple] = []

    def snapshot(self) -> Path:
        return self._snapshot

    def encode(self, prompt, negative_prompt, guidance, max_length):
        self.calls.append(("encode", prompt, negative_prompt, guidance, max_length))
        return "embeds", "negative"

    def load(self, mode):
        self.calls.append(("load", mode))
        return {"pipeline": "FakePipeline", "scheduler": "FakeScheduler", "flowShift": 5.0}

    def denoise(self, request, image, embeds, negative, on_step):
        self.calls.append(("denoise", request["mode"], None if image is None else image.size))
        self.request = request
        for step in range(request["steps"]):
            on_step(step + 1)
        return "latents"

    def decode(self, latents):
        frames, height, width = self.request["frames"], self.request["height"], self.request["width"]
        video = np.zeros((frames, height, width, 3), np.uint8)
        video[:, :, :, 0] = np.linspace(0, 255, frames, dtype=np.uint8)[:, None, None]
        return video

    def versions(self):
        return {"python": "test"}


def fake_writer(frames, path, fps, encode):
    path.write_bytes(b"mp4:" + json.dumps({"frames": len(frames), "fps": fps, **encode}).encode())


def request(tmp_path: Path, **overrides) -> dict:
    return {
        "mode": "t2v", "repo": "Wan-AI/Wan2.2-TI2V-5B-Diffusers", "revision": "b" * 40,
        "dtype": "bfloat16", "vaeDtype": "float32", "prompt": "a fox runs. the camera pans left.",
        "negativePrompt": "blurry", "image": None, "width": 64, "height": 32, "frames": 9, "fps": 24,
        "steps": 3, "guidance": 5.0, "seed": 42, "maxSequenceLength": 512,
        "encode": {"crf": 16}, "outputDir": str(tmp_path / "out"), "result": str(tmp_path / "out/result.json"),
        **overrides,
    }


def test_request_rules_follow_the_vae_and_patch_sizes(tmp_path):
    path = tmp_path / "request.json"
    for bad, message in (({"width": 48}, "32의 배수"), ({"frames": 10}, "4의 배수"),
                         ({"mode": "i2v", "image": str(tmp_path / "none.png")}, "첫 프레임"),
                         ({"prompt": ""}, "prompt")):
        path.write_text(json.dumps(request(tmp_path, **bad)))
        with pytest.raises(SystemExit, match=message):
            worker.load_request(path)
    path.write_text(json.dumps(request(tmp_path)))
    assert worker.load_request(path)["frames"] == 9


def test_partial_downloads_name_the_missing_weight_files(tmp_path):
    assert worker.missing_weight_files(fake_snapshot(tmp_path / "full")) == []
    missing = worker.missing_weight_files(
        fake_snapshot(tmp_path / "part", skip="transformer/diffusion_pytorch_model-00001-of-00001.safetensors"))
    assert missing == ["transformer/diffusion_pytorch_model-00001-of-00001.safetensors"]
    assert worker.missing_weight_files(tmp_path / "nothing") == ["model_index.json"]


def test_run_echoes_what_the_pipeline_received_and_keeps_key_frames(tmp_path, capsys):
    backend = FakeBackend(fake_snapshot(tmp_path / "snapshot"))
    result = worker.run(request(tmp_path), backend, writer=fake_writer)

    saved = json.loads((tmp_path / "out/result.json").read_text())
    assert saved == result
    assert result["sent"]["prompt"] == "a fox runs. the camera pans left."
    assert result["sent"]["negativePrompt"] == "blurry"
    assert result["sent"]["frames"] == 9 and result["sent"]["seed"] == 42
    assert backend.calls[0] == ("encode", "a fox runs. the camera pans left.", "blurry", 5.0, 512)
    assert set(result["seconds"]) == {"encode", "load", "denoise", "decode", "write", "total"}
    assert len(result["stepSeconds"]) == 3
    assert result["mpsPeakBytes"] == 4_000_000_000
    out = tmp_path / "out"
    assert (out / "video.mp4").read_bytes().startswith(b"mp4:")
    # 첫·가운데·끝 프레임을 따로 남긴다. 끝 프레임은 이어 만들기의 시작점이 된다.
    reds = [np.asarray(Image.open(out / f"{name}.png"))[0, 0, 0] for name in ("first", "middle", "last")]
    assert reds[0] == 0 and reds[2] == 255 and 0 < reds[1] < 255

    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith(worker.PROGRESS_PREFIX)]
    done = [json.loads(line[len(worker.PROGRESS_PREFIX):])["done"] for line in lines]
    assert done == sorted(done) and done[-1] == worker.PROGRESS_TOTAL


def test_image_mode_checks_the_first_frame_size(tmp_path):
    image = tmp_path / "input.png"
    Image.new("RGB", (64, 32), "red").save(image)
    backend = FakeBackend(fake_snapshot(tmp_path / "snapshot"))
    worker.run(request(tmp_path, mode="i2v", image=str(image)), backend, writer=fake_writer)
    assert ("denoise", "i2v", (64, 32)) in backend.calls

    Image.new("RGB", (60, 32), "red").save(image)
    with pytest.raises(SystemExit, match="크기"):
        worker.run(request(tmp_path, mode="i2v", image=str(image)), backend, writer=fake_writer)


def test_missing_weights_stop_before_any_model_loads(tmp_path):
    backend = FakeBackend(fake_snapshot(tmp_path / "snapshot", skip="vae/diffusion_pytorch_model.safetensors"))
    with pytest.raises(SystemExit, match="SETUP"):
        worker.run(request(tmp_path), backend, writer=fake_writer)
    assert backend.calls == []
