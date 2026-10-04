"""Stage time and peak memory across finished jobs.

These numbers are the evidence for hardware decisions: what the current Mac
handles comfortably, and where memory or time runs out.
"""

from __future__ import annotations

import statistics
import json
from typing import Any

from .jobs import JobStore


def variant(job: dict[str, Any], stage: dict[str, Any]) -> str:
    params = job["params"]
    if stage["name"] == "generate":
        # 모델을 빼면 모델을 바꾼 전후 기록이 한 줄에 섞여 중앙값이 뜻을 잃는다.
        model = params.get("imageModel") or "?"
        return f"{model} · {params.get('width')}x{params.get('height')} ×{params.get('count')}"
    if stage["name"] == "mesh":
        return f"{params.get('pipelineType')} · tex {params.get('textureSize')}"
    if stage["name"] == "post":
        return f"faces {params.get('targetFaces')}"
    if stage["name"] == "cutout" and (background := params.get("backgroundRemovalConfig")):
        return f"{background.get('repo')} @{str(background.get('revision') or '?')[:8]}"
    if stage["name"] == "video":
        model = params.get("videoModelConfig") or {}
        return (f"{params.get('videoModel', '?')} @{str(model.get('revision') or '?')[:8]} · "
                f"{params.get('width')}x{params.get('height')} · {params.get('frames')}f · "
                f"{params.get('steps')} steps · CFG {params.get('guidance')} · "
                f"{'t2v' if params.get('concept') is False else 'i2v'}")
    name = stage["name"]
    if name in ("surface", "lod", "texture-master", "texture-game", "post-master", "post-game",
                "optimize-master", "optimize-game", "inspect"):
        game = name.endswith("-game") or name == "lod"
        faces = params.get("gameFaces" if game else "targetFaces")
        texture = params.get("gameTextureSize" if game else "textureSize")
        return f"{params.get('pipelineType')} · faces {faces} · tex {texture} · size {params.get('sizeMeters')}"
    if stage["name"] == "previz":
        # 렌더러 선택이 프리비즈 비용을 가른다. 섞으면 비교할 수 없다.
        shots = len(params.get("shots") or [])
        label = f"{params.get('renderer')} · {params.get('width')}x{params.get('height')} · 샷 {shots}"
        if params.get("fps") is not None:
            duration = sum(float(shot.get("seconds") or 0) for shot in params.get("shots", []))
            label += f" · {duration:g}s · {params['fps']}fps · samples {params.get('samples')}"
        return label
    return ""


def execution_conditions(job: dict[str, Any], stage: dict[str, Any]) -> str:
    """Keep different execution snapshots apart even when display labels match."""
    params = job.get("params") or {}
    name = stage["name"]
    common = ("pipelineType", "meshModelConfig", "textureSize", "targetFaces", "gameFaces",
              "gameTextureSize", "sizeMeters", "processingVersion")
    if name == "generate":
        keys = ("imageModel", "imageModelConfig", "width", "height", "count")
    elif name == "video":
        keys = ("videoModel", "videoModelConfig", "width", "height", "frames", "fps", "steps", "guidance", "concept")
    elif name == "previz":
        keys = ("renderer", "width", "height", "fps", "samples", "shots", "assets", "clay", "aux", "animatic")
    elif name == "cutout":
        keys = ("backgroundRemovalConfig",)
    elif name in ("mesh", "post", "surface", "lod", "texture-master", "texture-game", "post-master", "post-game",
                  "optimize-master", "optimize-game", "inspect"):
        keys = common
    else:
        keys = ()
    values = {key: params.get(key) for key in keys}
    for key, value in list(values.items()):
        if isinstance(value, dict) and key.endswith("Config"):
            values[key] = {k: v for k, v in value.items() if k not in ("label", "hint", "license")}
    return json.dumps([job.get("recipe"), values], sort_keys=True, ensure_ascii=False)


def bench_rows(store: JobStore, limit: int = 10_000) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for job in store.list(limit=limit):
        for stage in job["stages"]:
            if stage.get("state") == "done" and stage.get("seconds"):
                groups.setdefault((stage["name"], variant(job, stage), execution_conditions(job, stage)), []).append(stage)
    rows = []
    for (name, label, _conditions), stages in sorted(groups.items()):
        seconds = [stage["seconds"] for stage in stages]
        peaks = [stage["peakMemoryBytes"] for stage in stages if stage.get("peakMemoryBytes")]
        rows.append({
            "stage": name,
            "label": stages[-1].get("label", name),
            "variant": label,
            "runs": len(stages),
            "medianSeconds": round(statistics.median(seconds), 1),
            "maxSeconds": round(max(seconds), 1),
            "maxPeakMemoryBytes": max(peaks) if peaks else None,
        })
    return rows
