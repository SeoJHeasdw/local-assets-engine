"""Stage time and peak memory across finished jobs.

These numbers are the evidence for hardware decisions: what the current Mac
handles comfortably, and where memory or time runs out.
"""

from __future__ import annotations

import statistics
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
    return ""


def bench_rows(store: JobStore, limit: int = 10_000) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for job in store.list(limit=limit):
        for stage in job["stages"]:
            if stage.get("state") == "done" and stage.get("seconds"):
                groups.setdefault((stage["name"], variant(job, stage)), []).append(stage)
    rows = []
    for (name, label), stages in sorted(groups.items()):
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
