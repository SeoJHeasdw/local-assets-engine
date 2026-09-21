from datetime import datetime, timezone

from local_assets_engine.estimates import estimate_jobs


def record(id, state="done", seconds=120, **params):
    return {"id": id, "recipe": "image", "params": {"imageModel": "flux", "width": 1024, "count": 1, **params},
            "state": state, "createdAt": f"2026-09-22T00:0{id}:00+00:00",
            "startedAt": "2026-09-22T01:00:00+00:00" if state == "running" else None,
            "stages": [{"state": "done", "seconds": seconds, "processes": [{}]}] if state == "done" else []}


def test_median_queue_wait_and_different_model_do_not_mix():
    jobs = [record("1", seconds=100), record("2", seconds=120), record("3", seconds=500),
            record("4", "running"), record("5", "queued"), record("6", "queued", imageModel="z-image")]
    result = estimate_jobs(jobs, now=datetime(2026, 9, 22, 1, 0, 30, tzinfo=timezone.utc))
    assert result["4"]["remainingSeconds"] == 90 and result["4"]["samples"] == 3
    assert result["5"]["waitSeconds"] == 90 and result["5"]["completionSeconds"] == 210
    assert result["6"]["remainingSeconds"] is None and result["6"]["samples"] == 0


def test_overdue_and_unknown_predecessors_do_not_promise_zero_remaining():
    jobs = [record(str(i)) for i in range(1, 4)] + [record("4", "running"), record("5", "queued")]
    result = estimate_jobs(jobs, now=datetime(2026, 9, 22, 1, 3, tzinfo=timezone.utc))
    assert result["4"]["overdue"] and result["4"]["remainingSeconds"] is None
    assert result["5"]["waitSeconds"] is None and result["5"]["completionSeconds"] is None


def test_failures_retries_downloads_and_insufficient_samples_are_excluded():
    jobs = [record(str(i)) for i in range(1, 6)] + [record("6", "queued")]
    jobs[0]["state"] = "failed"
    jobs[1]["stages"][0]["processes"] = [{}, {}]
    jobs[2]["logTail"] = ["Downloading weights"]
    result = estimate_jobs(jobs)["6"]
    assert result["samples"] == 2 and result["medianSeconds"] is None
