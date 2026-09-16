"""Previz recipe: blockout scene → camera rigs → sketch renders → shot records.

The product is the shot values, not the pictures. A genre is a shot preset, so
this recipe has no trailer-specific code: it resolves the preset's relative
framing against the placed assets and records what the camera actually did.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..jobs import JobNotFound
from ..presets import PresetError, find_shot_preset
from .base import Recipe, bool_param, choice_param, int_param

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

MESH_SUFFIXES = {".glb", ".gltf"}
MAX_ASSETS = 8
DEFAULT_PRESET = "game-trailer"


def _number(value: Any, name: str, low: float, high: float, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise PresetError(f"{name}는 숫자여야 합니다.") from error
    if not low <= number <= high:
        raise PresetError(f"{name}는 {low}~{high} 사이여야 합니다.")
    return number


def _placement(raw: dict[str, Any], index: int, store: "JobStore") -> dict[str, Any]:
    """One asset in the scene: a finished mesh job or a GLB file on disk."""
    source = raw.get("source")
    if source:
        job_id, asset_id = str(source.get("jobId")), str(source.get("assetId"))
        try:
            job = store.load(job_id)
            asset = next(a for a in job["assets"] if a["id"] == asset_id and a["kind"] == "mesh")
            file = store.resolve_file(job_id, asset["file"])
        except (JobNotFound, StopIteration) as error:
            raise PresetError("장면에 놓을 3D 에셋을 찾을 수 없습니다.") from error
        label = str(job.get("params", {}).get("subject") or job.get("title") or asset_id)
        reference: dict[str, str] | None = {"jobId": job_id, "assetId": asset_id}
    else:
        given = str(raw.get("path") or "")
        file = Path(given).expanduser()
        if not given or not file.is_absolute() or not file.is_file() or file.suffix.lower() not in MESH_SUFFIXES:
            raise PresetError("GLB·glTF 파일의 절대 경로가 필요합니다.")
        label, reference = file.stem, None
    position = raw.get("position") or (0.0, 0.0, 0.0)
    if len(position) != 3:
        raise PresetError("position은 [x, y, z] 세 값이어야 합니다.")
    return {
        "id": str(raw.get("id") or ("hero" if index == 0 else f"asset{index + 1}")),
        "label": label,
        "file": str(file),
        "source": reference,
        "position": [_number(value, "position", -1000.0, 1000.0, 0.0) for value in position],
        "yaw": _number(raw.get("yaw"), "yaw", -360.0, 360.0, 0.0),
        "scale": _number(raw.get("scale"), "scale", 0.01, 100.0, 1.0),
    }


def _prepare(params: dict[str, Any], presets: dict[str, Any], store: "JobStore") -> tuple[dict[str, Any], str]:
    previz = presets["previz"]
    defaults = previz["defaults"]
    entries = params.get("assets")
    if not entries and (params.get("source") or params.get("path")):
        entries = [{"source": params.get("source"), "path": params.get("path")}]
    if not entries:
        raise PresetError("장면에 놓을 3D 에셋이 필요합니다.")
    if len(entries) > MAX_ASSETS:
        raise PresetError(f"한 장면에 에셋은 {MAX_ASSETS}개까지 놓을 수 있습니다.")
    assets = [_placement(entry, index, store) for index, entry in enumerate(entries)]
    if len({asset["id"] for asset in assets}) != len(assets):
        raise PresetError("에셋 id가 중복됩니다.")

    preset = find_shot_preset(presets, str(params.get("preset") or DEFAULT_PRESET))
    # 프리셋은 "hero"라는 이름으로 주인공을 가리킨다. 장면의 첫 에셋이 그 자리다.
    known = {asset["id"] for asset in assets} | {"scene"}
    hero = assets[0]["id"]
    shots = []
    for order, shot in enumerate(preset["shots"], start=1):
        focus = str(shot.get("focus", "scene"))
        if focus == "hero":
            focus = hero
        if focus not in known:
            raise PresetError(f"{shot['id']}의 focus를 장면에서 찾을 수 없습니다: {focus}")
        shots.append({**shot, "focus": focus, "order": order})

    clay = bool_param(params, "clay", defaults["clay"])
    normalized = {
        "preset": preset["id"],
        "assets": assets,
        "shots": shots,
        "hero": hero,
        "renderer": choice_param(params, "renderer", defaults["renderer"], previz["renderers"]),
        "width": int_param(params, "width", defaults["width"], 256, 1920),
        "height": int_param(params, "height", defaults["height"], 144, 1080),
        "fps": int_param(params, "fps", defaults["fps"], 6, 30),
        "samples": int_param(params, "samples", defaults["samples"], 1, 256),
        "aux": choice_param(params, "aux", defaults["aux"], previz["auxModes"]),
        "animatic": bool_param(params, "animatic", defaults["animatic"]),
        "ground": bool_param(params, "ground", defaults["ground"]),
        "clay": clay,
        "look": {**preset.get("look", {}), "clay": clay},
    }
    return normalized, f"프리비즈 · {preset['label']} · {assets[0]['label']}"


def _run(ctx: "JobContext") -> None:
    p = ctx.params
    previz_dir = ctx.dir / "previz"
    previz_dir.mkdir(exist_ok=True)
    plan_path, result_path = previz_dir / "plan.json", previz_dir / "shots.json"
    plan = {
        key: p[key] for key in
        ("renderer", "width", "height", "fps", "samples", "aux", "animatic", "ground", "look", "shots")
    }
    plan["assets"] = [{key: asset[key] for key in ("id", "file", "position", "yaw", "scale")}
                      for asset in p["assets"]]
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")

    with ctx.stage("previz", "프리비즈 스케치 렌더 (Blender)") as stage:
        stage.progress(0, f"{p['renderer']} · 샷 {len(p['shots'])}개", force=True)
        stage.run([
            sys.executable, "-m", "local_assets_engine.tools.previz_render",
            "--plan", plan_path, "--out-dir", previz_dir, "--result", result_path,
        ], cwd=previz_dir)

    result = json.loads(result_path.read_text("utf-8"))
    for shot in result["shots"]:
        files = shot["files"]
        key_frame = previz_dir / files["key"]
        clip = previz_dir / files["animatic"] if files.get("animatic") else None
        ctx.add_asset(
            kind="shot", role="candidate", file=clip or key_frame, preview=key_frame,
            meta={
                "shot": shot["id"], "label": shot["label"], "purpose": shot["purpose"],
                "move": shot["move"], "focus": shot["focus"], "order": shot["order"],
                "seconds": shot["seconds"], "fps": shot["fps"], "frames": shot["frames"],
                "ease": shot["ease"], "lens": shot["lens"], "lensEnd": shot["lensEnd"],
                "sensorWidth": shot["sensorWidth"], "start": shot["start"], "end": shot["end"],
                "framing": shot["framing"], "depthRange": shot["depthRange"],
                "renderer": result["renderer"], "resolution": result["resolution"],
                "clay": result["clay"], "renderSeconds": shot["renderSeconds"],
                "preset": p["preset"],
                # 프레임별 카메라 값은 job.json을 불리므로 파일에 두고 여기서 가리킨다.
                "pathFile": ctx.rel(result_path),
                "files": {name: ([ctx.rel(previz_dir / item) for item in value]
                                 if isinstance(value, list) else
                                 ctx.rel(previz_dir / value) if value else None)
                          for name, value in files.items()},
            },
        )


PREVIZ = Recipe(id="previz", label="프리비즈 샷", prepare=_prepare, run=_run)
