from local_assets_engine.measure import StageFailed
from local_assets_engine.recipes.mesh import explain_trellis_failure


def failure(*tail, code=1):
    return StageFailed("종료 코드 1", code=code, tail=list(tail))


def test_a_killed_process_is_explained_as_memory_pressure():
    message = explain_trellis_failure(failure("time: signal: Invalid argument", "warnings.warn(...)"))
    assert "메모리" in message and "512" in message


def test_missing_dinov3_access_points_at_the_setup_step():
    message = explain_trellis_failure(failure("huggingface_hub.errors.GatedRepoError: 403"))
    assert "DINOv3" in message and "SETUP" in message


def test_the_gpu_watchdog_is_explained_separately():
    message = explain_trellis_failure(failure("kIOGPUCommandBufferCallbackErrorTimeout", code=2))
    assert "GPU" in message


def test_anything_else_keeps_the_original_message():
    assert "TRELLIS.2" in explain_trellis_failure(failure("모델 파일이 깨졌습니다"))
