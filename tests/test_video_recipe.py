import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from local_assets_engine import doctor
from local_assets_engine.estimates import condition_key
from local_assets_engine.jobs import JobStore
from local_assets_engine.library import classify_job_files
from local_assets_engine.measure import StageFailed
from local_assets_engine.presets import PresetError, load_presets
from local_assets_engine.recipes import video
from local_assets_engine.runner import Runner

TESTS = Path(__file__).resolve().parent
FAKE_WORKER = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(TESTS)!r})
from test_video_worker import FakeBackend, fake_snapshot, fake_writer
from local_assets_engine.workers import video_runner as worker

request = worker.load_request(Path(sys.argv[-1]))
snapshot = fake_snapshot(Path(request["outputDir"]) / "snapshot")
worker.run(request, FakeBackend(snapshot), writer=fake_writer)
"""


def presets():
    return load_presets()


def write_presets(tmp_path, change):
    data = json.loads(Path(video.__file__).resolve().parents[3].joinpath("config/presets.json").read_text())
    change(data)
    path = tmp_path / "presets.json"
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


def test_repository_video_model_is_pinned_to_the_model_card_defaults():
    model = presets()["videoModel"]
    assert model["repo"] == "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
    assert model["revision"] == "b8fff7315c768468a5333511427288870b2e9635"
    assert (model["width"], model["height"], model["frames"], model["fps"]) == (1280, 704, 121, 24)
    assert (model["steps"], model["guidance"], model["vaeDtype"]) == (50, 5.0, "float32")


def test_config_rejects_unpinned_revisions_and_impossible_defaults(tmp_path):
    for change, message in (
        (lambda d: d["videoModel"].update(revision="main"), "커밋 해시"),
        (lambda d: d["videoModel"].update(frames=120), "4의 배수"),
        (lambda d: d["videoModel"].update(width=1300), "32의 배수"),
        (lambda d: d["video"]["camera"]["move"].append({"id": "static", "text": "x"}), "중복"),
    ):
        with pytest.raises(PresetError, match=message):
            load_presets(write_presets(tmp_path, change))


def test_composition_goes_into_the_first_frame_and_movement_only_into_the_video():
    params, title = video._prepare_text_to_video({
        "subject": "a red fox in a snowy forest", "motion": "the fox trots toward the camera",
        "camera": {"shot": "wide", "angle": "low", "move": "dolly-in"}, "style": "golden hour", "seed": 7,
    }, presets(), None)
    assert title == "영상 · a red fox in a snowy forest"
    assert "wide shot" in params["prompt"] and "low-angle" in params["prompt"]
    assert "dollies" not in params["prompt"] and "golden hour" in params["prompt"]
    assert params["videoPrompt"] == (
        "a red fox in a snowy forest. the fox trots toward the camera. "
        "wide shot, low-angle shot looking up, the camera slowly dollies in toward the subject. "
        "cinematic live-action film, photorealistic, natural lighting, golden hour.")
    assert params["videoPromptEdited"] is False
    assert params["videoPromptParts"]["camera"].startswith("wide shot")
    assert (params["width"], params["height"], params["frames"], params["count"]) == (1280, 704, 121, 1)
    assert params["removeBackground"] is False and params["concept"] is True
    assert params["seeds"] == [7] and params["videoSeed"] == 7
    assert params["negativePrompt"] == presets()["videoModel"]["negativePrompt"]
    assert params["videoModelConfig"]["revision"] == presets()["videoModel"]["revision"]


def test_an_edited_prompt_is_sent_verbatim_and_settings_are_snapped():
    params, _ = video._prepare_text_to_video({
        "subject": "fox", "videoPrompt": "  A fox.   The camera orbits.  ", "negativePrompt": "",
        "width": 1000, "height": 500, "frames": 50, "steps": 20, "guidance": 3.5, "concept": False,
    }, presets(), None)
    assert params["videoPrompt"] == "A fox. The camera orbits." and params["videoPromptEdited"] is True
    assert params["videoPromptParts"]["subject"] == "fox"
    assert (params["width"], params["height"], params["frames"]) == (992, 480, 49)
    assert (params["steps"], params["guidance"], params["negativePrompt"], params["concept"]) == (20, 3.5, "", False)
    assert params["seconds"] == round(49 / 24, 2)


def test_bad_video_requests_are_rejected_before_queueing():
    for bad, message in (
        ({"subject": ""}, "무엇을"),
        ({"subject": "fox", "camera": {"move": "barrel-roll"}}, "카메라"),
        ({"subject": "fox", "camera": {"zoom": "in"}}, "camera"),
        ({"subject": "fox", "width": 1280, "height": 1280}, "픽셀"),
        ({"subject": "fox", "preset": "item-icon"}, "video"),
        ({"subject": "fox", "videoPrompt": "x" * 2001}, "2000자"),
    ):
        with pytest.raises(PresetError, match=message):
            video._prepare_text_to_video(bad, presets(), None)


def image_job(store: JobStore, size=(800, 1200)) -> dict:
    job = store.create("image", {"subject": "knight"}, "fixture")
    folder = store.job_dir(job["id"]) / "final"
    folder.mkdir(parents=True)
    Image.new("RGBA", size, (200, 30, 30, 255)).save(folder / "seed-1.png")
    asset = {"id": "a01", "kind": "image", "file": "final/seed-1.png", "meta": {}, "review": "pending"}
    store.update(job["id"], lambda value: value.update(state="done", assets=[asset],
                                                       params={"subject": "knight"}))
    return {"source": {"jobId": job["id"], "assetId": "a01"}}


def test_image_to_video_follows_the_source_orientation_and_lineage(tmp_path):
    store = JobStore(tmp_path / "jobs")
    request = image_job(store)
    params, title = video._prepare_image_to_video({**request, "motion": "the knight raises a sword"}, presets(), store)
    assert title == "영상 · knight" and params["source"] == request["source"]
    assert (params["width"], params["height"]) == (704, 1280)
    assert params["videoPrompt"] == "knight. the knight raises a sword."
    with pytest.raises(PresetError, match="찾을 수 없습니다"):
        video._prepare_image_to_video({"source": {"jobId": "nope", "assetId": "a01"}}, presets(), store)


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    script = tmp_path / "fake_video_worker.py"
    script.write_text(FAKE_WORKER)
    monkeypatch.setattr(video, "video_python", lambda: Path(sys.executable))
    monkeypatch.setattr(video, "WORKER", script)
    store = JobStore(tmp_path / "jobs")
    return store, Runner(store, recipes={r.id: r for r in (video.TEXT_TO_VIDEO, video.IMAGE_TO_VIDEO)})


def test_image_to_video_runs_measured_and_records_the_sent_prompt(fake_engine):
    store, runner = fake_engine
    request = image_job(store, size=(1200, 800))
    job = runner.run_job(runner.create("image-to-video", {
        **request, "motion": "the knight raises a sword", "camera": {"move": "orbit"},
        "width": 384, "height": 256, "frames": 9, "steps": 3, "seed": 5,
    })["id"])
    assert job["state"] == "done", job["error"]
    stage = next(s for s in job["stages"] if s["name"] == "video")
    assert stage["seconds"] > 0 and stage["peakMemoryBytes"] > 0 and stage["progress"] == 1.0
    [asset] = job["assets"]
    meta = asset["meta"]
    assert asset["kind"] == "video" and asset["review"] == "pending" and asset["file"] == "video/video.mp4"
    assert meta["prompt"] == job["params"]["videoPrompt"] == (
        "knight. the knight raises a sword. the camera orbits around the subject.")
    assert meta["mode"] == "i2v" and meta["seed"] == 5 and meta["frames"] == 9
    assert meta["source"] == request["source"] and meta["conceptAsset"] is None
    # 3:2 원본을 3:2 목표로 늘리지 않고 그대로 줄인다.
    assert meta["crop"] == {"x": 0, "y": 0, "width": 1200, "height": 800}
    with Image.open(store.resolve_file(job["id"], meta["inputImage"])) as first:
        assert first.size == (384, 256) and first.mode == "RGB"
    files = {item["path"]: item["category"] for item in classify_job_files(store.job_dir(job["id"]), job)}
    assert files["video/video.mp4"] == "results" and files["video/middle.png"] == "previews"
    assert files["video/input.png"] == "bases"
    assert files["video/request.json"] == files["video/result.json"] == "records"


def test_text_to_video_animates_its_concept_or_uses_text_mode(fake_engine, monkeypatch):
    store, runner = fake_engine

    def concept(ctx, p, *, role):
        path = ctx.dir / "concept.png"
        Image.new("RGB", (p["width"], p["height"]), "blue").save(path)
        asset = ctx.add_asset(kind="image", role=role, file=path, preview=path, meta={"prompt": p["prompt"]})
        return [{"file": path, "error": None, "checks": {}, "asset": asset}]

    monkeypatch.setattr(video, "generate_candidates", concept)
    base = {"subject": "fox", "width": 256, "height": 256, "frames": 5, "steps": 2, "seed": 3}
    job = runner.run_job(runner.create("text-to-video", base)["id"])
    assert job["state"] == "done", job["error"]
    concept_asset, clip = job["assets"]
    assert concept_asset["role"] == "concept" and clip["meta"]["conceptAsset"] == concept_asset["id"]
    assert clip["meta"]["mode"] == "i2v"

    job = runner.run_job(runner.create("text-to-video", {**base, "concept": False})["id"])
    assert job["state"] == "done", job["error"]
    [clip] = job["assets"]
    assert clip["meta"]["mode"] == "t2v" and clip["meta"]["inputImage"] is None


def test_a_missing_video_environment_points_at_the_setup_script(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "video_python", lambda: tmp_path / "missing/python")
    store = JobStore(tmp_path / "jobs")
    runner = Runner(store, recipes={video.TEXT_TO_VIDEO.id: video.TEXT_TO_VIDEO})
    job = runner.run_job(runner.create("text-to-video", {"subject": "fox", "concept": False})["id"])
    assert job["state"] == "failed" and "setup_video.sh" in job["error"]


def test_failures_are_explained_in_terms_the_user_can_act_on():
    def failure(*tail, code=1):
        return StageFailed("종료 코드 1", code=code, tail=list(tail))

    assert "메모리" in video.explain_video_failure(failure("time: signal: Killed"))
    assert "MPS 메모리" in video.explain_video_failure(failure("RuntimeError: MPS backend out of memory (MPS allocated: 30 GB)"))
    message = "영상 모델 가중치가 다 받아지지 않았습니다: vae/x. docs/SETUP.md의 내려받기 명령을 다시 실행하면 이어서 받습니다."
    assert video.explain_video_failure(failure("Traceback", message)) == message
    assert "GPU" in video.explain_video_failure(failure("kIOGPUCommandBufferCallbackErrorTimeout"))
    assert "영상 생성이 실패" in video.explain_video_failure(failure("ValueError: boom"))


def test_estimates_group_video_jobs_by_what_changes_run_time():
    params, _ = video._prepare_text_to_video({"subject": "fox", "seed": 1}, presets(), None)
    renamed = {**params, "videoModelConfig": {**params["videoModelConfig"], "label": "새 이름"}}
    fewer = {**params, "frames": 49}
    key = condition_key({"recipe": "text-to-video", "params": params})
    assert key == condition_key({"recipe": "text-to-video", "params": renamed})
    assert key != condition_key({"recipe": "text-to-video", "params": fewer})


def test_doctor_counts_only_complete_pinned_snapshots(tmp_path, monkeypatch):
    from test_video_worker import fake_snapshot

    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    repo, revision = "Wan-AI/Wan2.2-TI2V-5B-Diffusers", "b" * 40
    folder = tmp_path / "models--Wan-AI--Wan2.2-TI2V-5B-Diffusers"
    assert doctor.snapshot_missing(repo, revision) == ["model_index.json"]
    fake_snapshot(folder / "snapshots" / revision, skip="vae/diffusion_pytorch_model.safetensors")
    assert doctor.snapshot_missing(repo, revision) == ["vae/*.safetensors"]
    (folder / "snapshots" / revision / "vae/diffusion_pytorch_model.safetensors").write_text("")
    assert doctor.snapshot_missing(repo, revision) == []
    (folder / "blobs").mkdir()
    (folder / "blobs/abc.incomplete").write_text("")
    assert doctor.snapshot_missing(repo, revision) != []
