"""Presets: prompt templates and defaults shared by the UI, CLI and recipes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import PRESETS_PATH

KINDS = ("2d", "3d")


class PresetError(ValueError):
    """Invalid preset file or a request that no preset can satisfy."""


def load_presets(path: Path = PRESETS_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text("utf-8"))
    seen: set[str] = set()
    for preset in data.get("presets", []):
        preset_id = preset.get("id")
        if not preset_id or preset_id in seen:
            raise PresetError(f"프리셋 id가 비었거나 중복됩니다: {preset_id!r}")
        seen.add(preset_id)
        if preset.get("kind") not in KINDS:
            raise PresetError(f"{preset_id}: kind는 {KINDS} 중 하나여야 합니다.")
        if "{subject}" not in preset.get("prompt", ""):
            raise PresetError(f"{preset_id}: prompt에 {{subject}} 자리가 없습니다.")
        if preset.get("pixelate") and not preset.get("removeBackground"):
            raise PresetError(f"{preset_id}: 픽셀화는 배경 제거 뒤에만 쓸 수 있습니다.")
    seen.clear()
    for preset in data.get("previz", {}).get("shotPresets", []):
        preset_id = preset.get("id")
        if not preset_id or preset_id in seen:
            raise PresetError(f"샷 프리셋 id가 비었거나 중복됩니다: {preset_id!r}")
        seen.add(preset_id)
        shots = preset.get("shots") or []
        if not shots:
            raise PresetError(f"{preset_id}: 샷이 비어 있습니다.")
        # 샷 id는 결과 폴더 이름이 된다.
        if len({shot.get("id") for shot in shots}) != len(shots):
            raise PresetError(f"{preset_id}: 샷 id가 중복됩니다.")
        for shot in shots:
            if not shot.get("id") or float(shot.get("seconds", 0)) <= 0:
                raise PresetError(f"{preset_id}: 샷에는 id와 0보다 큰 seconds가 필요합니다.")
    return data


def find_preset(data: dict[str, Any], preset_id: str, *, kind: str | None = None) -> dict[str, Any]:
    for preset in data.get("presets", []):
        if preset["id"] == preset_id:
            if kind and preset["kind"] != kind:
                raise PresetError(f"{preset_id}는 {kind} 프리셋이 아닙니다.")
            return preset
    raise PresetError(f"알 수 없는 프리셋입니다: {preset_id}")


def find_shot_preset(data: dict[str, Any], preset_id: str) -> dict[str, Any]:
    for preset in data.get("previz", {}).get("shotPresets", []):
        if preset["id"] == preset_id:
            return preset
    raise PresetError(f"알 수 없는 샷 프리셋입니다: {preset_id}")


def build_prompt(preset: dict[str, Any], subject: str, style: str = "") -> str:
    subject = " ".join(str(subject or "").split())
    if not subject:
        raise PresetError("무엇을 만들지 적어 주세요.")
    prompt = preset["prompt"].replace("{subject}", subject)
    style = " ".join(str(style or "").split())
    return f"{prompt}, {style}" if style else prompt
