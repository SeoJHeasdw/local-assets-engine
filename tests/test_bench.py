from local_assets_engine.bench import bench_rows, variant
from local_assets_engine.jobs import JobStore


def stage(name, seconds, peak=None, label=None):
    return {"name": name, "label": label or name, "state": "done", "seconds": seconds,
            "peakMemoryBytes": peak, "processes": []}


def test_generate_rows_are_grouped_per_model():
    job = {"params": {"imageModel": "flux2-klein-4b", "width": 1024, "height": 1024, "count": 2}}
    assert variant(job, stage("generate", 1)) == "flux2-klein-4b · 1024x1024 ×2"
    older = {"params": {"imageModel": "z-image-turbo", "width": 1024, "height": 1024, "count": 2}}
    assert variant(older, stage("generate", 1)) != variant(job, stage("generate", 1))


def test_mesh_and_post_rows_carry_their_own_settings():
    job = {"params": {"pipelineType": "512", "textureSize": 1024, "targetFaces": 30000}}
    assert variant(job, stage("mesh", 1)) == "512 · tex 1024"
    assert variant(job, stage("post", 1)) == "faces 30000"
    assert variant(job, stage("cutout", 1)) == ""


def test_previz_rows_separate_the_renderers():
    job = {"params": {"renderer": "eevee", "width": 960, "height": 540, "shots": [{}, {}]}}
    assert variant(job, stage("previz", 1)) == "eevee · 960x540 · 샷 2"
    cheap = {"params": {"renderer": "workbench", "width": 960, "height": 540, "shots": [{}, {}]}}
    assert variant(cheap, stage("previz", 1)) != variant(job, stage("previz", 1))


def test_rows_summarise_time_and_peak_memory(tmp_path):
    store = JobStore(tmp_path)
    params = {"imageModel": "flux2-klein-4b", "width": 1024, "height": 1024, "count": 2}
    for seconds, peak in [(40.0, 20 * 2**30), (60.0, 24 * 2**30)]:
        job = store.create("image", params, "t")
        store.update(job["id"], lambda record, s=seconds, p=peak: record.update(
            state="done", stages=[stage("generate", s, p, "이미지 생성")]))
    rows = bench_rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert (row["runs"], row["medianSeconds"], row["maxSeconds"]) == (2, 50.0, 60.0)
    assert row["maxPeakMemoryBytes"] == 24 * 2**30
    assert row["label"] == "이미지 생성"
