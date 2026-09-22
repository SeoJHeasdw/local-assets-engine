"""Conservative duration estimates from comparable, successful measured jobs."""

from __future__ import annotations

import json
import statistics
from datetime import datetime
from typing import Any

MIN_SAMPLES = 3
IMAGE_FIELDS = ("imageModel", "imageModelConfig", "width", "height", "count", "removeBackground", "canvas", "pixelate")
MESH_FIELDS = ("pipelineType", "textureSize", "targetFaces", "gameFaces", "gameTextureSize", "removeBackground")
VIDEO_FIELDS = ("videoModel", "videoModelConfig", "width", "height", "frames", "steps", "guidance")


def condition_key(job: dict[str, Any]) -> str | None:
    recipe, params = job["recipe"], job.get("params") or {}
    fields = {
        "image": IMAGE_FIELDS,
        "text-to-3d": IMAGE_FIELDS + MESH_FIELDS,
        "image-to-3d": MESH_FIELDS,
        "refine-mesh": MESH_FIELDS,
        "text-to-video": IMAGE_FIELDS + VIDEO_FIELDS + ("concept",),
        "image-to-video": VIDEO_FIELDS,
    }.get(recipe)
    if fields is None:
        return None
    values = {name: params.get(name) for name in fields}
    # 표시 이름·힌트가 바뀌어도 실행 조건은 같다.
    for name in ("imageModelConfig", "videoModelConfig"):
        if isinstance(values.get(name), dict):
            values[name] = {k: v for k, v in values[name].items() if k not in ("label", "hint", "license")}
    return json.dumps([recipe, values], sort_keys=True, ensure_ascii=False)


def estimate_jobs(jobs: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    groups: dict[str, list[float]] = {}
    for job in jobs:
        key = condition_key(job)
        stages = job.get("stages") or []
        if not key or job["state"] != "done" or not stages:
            continue
        if any(stage.get("state") not in ("done", "skipped") or len(stage.get("processes", [])) > 1 for stage in stages):
            continue
        if any("Downloading" in line or "내려받" in line for line in job.get("logTail", [])):
            continue
        duration = sum(stage.get("seconds") or 0 for stage in stages)
        if duration > 0:
            groups.setdefault(key, []).append(duration)

    active = sorted((job for job in jobs if job["state"] in ("queued", "running", "cancelling")),
                    key=lambda job: (job["state"] == "queued", job["createdAt"], job["id"]))
    results = {}
    queue_seconds: float | None = 0
    for job in active:
        samples = groups.get(condition_key(job), [])
        total = statistics.median(samples) if len(samples) >= MIN_SAMPLES else None
        queued = job["state"] == "queued"
        elapsed = 0
        if job.get("startedAt"):
            elapsed = max(0, (now - datetime.fromisoformat(job["startedAt"])).total_seconds())
        overdue = total is not None and not queued and elapsed >= total
        remaining = None if total is None or overdue or job["state"] == "cancelling" else max(0, total - elapsed)
        wait = queue_seconds if queued else 0
        results[job["id"]] = {
            "samples": len(samples), "medianSeconds": total, "remainingSeconds": remaining,
            "waitSeconds": wait, "overdue": overdue,
            "completionSeconds": wait + remaining if wait is not None and remaining is not None else None,
        }
        queue_seconds = queue_seconds + remaining if queue_seconds is not None and remaining is not None else None
    return results
