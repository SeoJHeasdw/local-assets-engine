"""Previz recipe: blockout scene → camera rigs → sketch renders → shot records.

The product is the shot values, not the pictures. A genre is a shot preset, so
this recipe has no trailer-specific code: it resolves the preset's relative
framing against the placed assets and records what the camera actually did.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..jobs import JobNotFound
from ..presets import PresetError, find_shot_preset, find_standin
from .base import Recipe, bool_param, choice_param, int_param

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

MESH_SUFFIXES = {".glb", ".gltf"}
MAX_ASSETS = 8
MAX_SHOTS = 24
DEFAULT_PRESET = "game-trailer"
# 샷 id는 결과 폴더 이름이 되고 에셋 id는 Blender 오브젝트 이름이 된다. 경로 문자를 받지 않는다.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
FRAMING_RANGES = {
    "distance": (0.2, 20.0), "azimuth": (-720.0, 720.0), "height": (-1.0, 10.0),
    "targetHeight": (-1.0, 5.0), "roll": (-90.0, 90.0),
}


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


def _standin(raw: dict[str, Any], presets: dict[str, Any]) -> dict[str, Any]:
    """A gray stand-in from the catalog, stretched per axis to the size asked for."""
    kind = find_standin(presets, str(raw["standin"]))
    given = raw.get("size") or kind["size"]
    if not isinstance(given, list) or len(given) != 3:
        raise PresetError("대역 size는 [가로, 깊이, 높이] 세 값이어야 합니다.")
    size = [_number(value, "대역 size", 0.05, 500.0, float(base)) for value, base in zip(given, kind["size"])]
    stretch = [new / float(base) for new, base in zip(size, kind["size"])]
    # 부품은 카탈로그 치수 기준 미터다. 늘인 결과를 기록에 남겨 카탈로그가 바뀌어도 다시 그릴 수 있다.
    parts = [{
        "shape": part["shape"], "axis": part.get("axis", "z"),
        "at": [round(float(value) * factor, 4) for value, factor in zip(part["at"], stretch)],
        "size": [round(float(value) * factor, 4) for value, factor in zip(part["size"], stretch)],
    } for part in kind["parts"]]
    return {"standin": kind["id"], "label": f"{kind['label']} 대역", "size": size, "parts": parts}


def _placement(raw: Any, index: int, store: "JobStore", presets: dict[str, Any]) -> dict[str, Any]:
    """One thing in the scene: a finished mesh job, a GLB file on disk, or a stand-in."""
    if not isinstance(raw, dict):
        raise PresetError("배치 항목 형식이 올바르지 않습니다.")
    source = raw.get("source")
    standin: dict[str, Any] = {}
    file: Path | None = None
    reference: dict[str, str] | None = None
    if raw.get("standin"):
        standin = _standin(raw, presets)
        label = standin.pop("label")
    elif source:
        job_id, asset_id = str(source.get("jobId")), str(source.get("assetId"))
        try:
            job = store.load(job_id)
            asset = next(a for a in job["assets"] if a["id"] == asset_id and a["kind"] == "mesh")
            file = store.resolve_file(job_id, asset["file"])
        except (JobNotFound, StopIteration) as error:
            raise PresetError("장면에 놓을 3D 에셋을 찾을 수 없습니다.") from error
        label = str(job.get("params", {}).get("subject") or job.get("title") or asset_id)
        reference = {"jobId": job_id, "assetId": asset_id}
    else:
        given = str(raw.get("path") or "")
        file = Path(given).expanduser()
        if not given or not file.is_absolute() or not file.is_file() or file.suffix.lower() not in MESH_SUFFIXES:
            raise PresetError("GLB·glTF 파일의 절대 경로가 필요합니다.")
        label = file.stem
    position = raw.get("position") or (0.0, 0.0, 0.0)
    if len(position) != 3:
        raise PresetError("position은 [x, y, z] 세 값이어야 합니다.")
    asset_id = str(raw.get("id") or ("hero" if index == 0 else f"asset{index + 1}"))
    if not SAFE_ID.match(asset_id):
        raise PresetError("에셋 id는 영문·숫자·-·_ 32자 이내여야 합니다.")
    return {
        "id": asset_id,
        "label": label,
        "file": str(file) if file else None,
        "source": reference,
        **standin,
        "position": [_number(value, "position", -1000.0, 1000.0, 0.0) for value in position],
        "yaw": _number(raw.get("yaw"), "yaw", -360.0, 360.0, 0.0),
        "scale": _number(raw.get("scale"), "scale", 0.01, 100.0, 1.0),
    }


def _edited_shots(raw: Any, known: set[str], hero: str) -> list[dict[str, Any]]:
    """Shots a person rearranged or re-aimed in the app, checked like preset values."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SHOTS:
        raise PresetError(f"컷은 1~{MAX_SHOTS}개여야 합니다.")
    shots: list[dict[str, Any]] = []
    for order, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise PresetError("컷 형식이 올바르지 않습니다.")
        shot_id = str(item.get("id") or "")
        if not SAFE_ID.match(shot_id) or any(shot["id"] == shot_id for shot in shots):
            raise PresetError("컷 id는 영문·숫자·-·_ 32자 이내이고 겹치지 않아야 합니다.")
        given = item.get("framing") if isinstance(item.get("framing"), dict) else {}
        framing: dict[str, Any] = {}
        for key, (low, high) in FRAMING_RANGES.items():
            for name in (key, f"{key}End"):
                if given.get(name) not in (None, ""):
                    framing[name] = _number(given[name], f"{shot_id} {name}", low, high, 0.0)
        if given.get("targetOffset") is not None:
            offset = given["targetOffset"]
            if not isinstance(offset, list) or len(offset) != 2:
                raise PresetError(f"{shot_id} targetOffset은 [x, y] 두 값이어야 합니다.")
            framing["targetOffset"] = [_number(value, f"{shot_id} targetOffset", -5.0, 5.0, 0.0) for value in offset]
        focus = str(item.get("focus") or "scene")
        focus = hero if focus == "hero" else focus
        if focus not in known:
            raise PresetError(f"{shot_id}의 focus를 장면에서 찾을 수 없습니다: {focus}")
        shot = {
            "id": shot_id,
            "label": str(item.get("label") or shot_id)[:40],
            "purpose": str(item.get("purpose") or "")[:80],
            "move": str(item.get("move") or "static")[:24],
            "focus": focus,
            "lens": _number(item.get("lens"), f"{shot_id} lens", 8.0, 300.0, 35.0),
            "seconds": _number(item.get("seconds"), f"{shot_id} seconds", 0.2, 60.0, 2.0),
            "ease": "linear" if item.get("ease") == "linear" else "inout",
            "framing": framing,
            "order": order,
        }
        if item.get("lensEnd") not in (None, ""):
            shot["lensEnd"] = _number(item["lensEnd"], f"{shot_id} lensEnd", 8.0, 300.0, shot["lens"])
        shots.append(shot)
    return shots


def _prepare(params: dict[str, Any], presets: dict[str, Any], store: "JobStore") -> tuple[dict[str, Any], str]:
    previz = presets["previz"]
    defaults = previz["defaults"]
    entries = params.get("assets")
    if not entries and (params.get("source") or params.get("path")):
        entries = [{"source": params.get("source"), "path": params.get("path")}]
    if not entries:
        raise PresetError("장면에 놓을 3D 에셋이나 대역이 필요합니다.")
    if not isinstance(entries, list):
        raise PresetError("assets는 배치 항목의 목록이어야 합니다.")
    if len(entries) > MAX_ASSETS:
        raise PresetError(f"한 장면에 에셋과 대역은 합쳐서 {MAX_ASSETS}개까지 놓을 수 있습니다.")
    assets = [_placement(entry, index, store, presets) for index, entry in enumerate(entries)]
    if len({asset["id"] for asset in assets}) != len(assets):
        raise PresetError("에셋 id가 중복됩니다.")

    preset = find_shot_preset(presets, str(params.get("preset") or DEFAULT_PRESET))
    # 프리셋은 "hero"라는 이름으로 주인공을 가리킨다. 장면의 첫 에셋이 그 자리다.
    known = {asset["id"] for asset in assets} | {"scene"}
    hero = assets[0]["id"]
    edited = bool(params.get("shots"))
    if edited:
        shots = _edited_shots(params["shots"], known, hero)
    else:
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
        "edited": edited,
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
    label = f"{preset['label']} 편집" if edited else preset["label"]
    return normalized, f"프리비즈 · {label} · {assets[0]['label']}"


def _run(ctx: "JobContext") -> None:
    p = ctx.params
    previz_dir = ctx.dir / "previz"
    previz_dir.mkdir(exist_ok=True)
    plan_path, result_path = previz_dir / "plan.json", previz_dir / "shots.json"
    plan = {
        key: p[key] for key in
        ("renderer", "width", "height", "fps", "samples", "aux", "animatic", "ground", "look", "shots")
    }
    plan["assets"] = [{key: asset.get(key) for key in ("id", "file", "standin", "parts", "position", "yaw", "scale")}
                      for asset in p["assets"]]
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")

    with ctx.stage("previz", "프리비즈 스케치 렌더 (Blender)") as stage:
        stage.progress(0, f"{p['renderer']} · 샷 {len(p['shots'])}개", force=True)
        stage.run([
            sys.executable, "-m", "local_assets_engine.tools.previz_render",
            "--plan", plan_path, "--out-dir", previz_dir, "--result", result_path,
        ], cwd=previz_dir)

    result = json.loads(result_path.read_text("utf-8"))
    sequence = result.get("sequence")
    placement = {cut["shot"]: cut for cut in (sequence or {}).get("cuts", [])}
    for shot in result["shots"]:
        files = shot["files"]
        key_frame = previz_dir / files["key"]
        clip = previz_dir / files["animatic"] if files.get("animatic") else None
        # 컷마다 이어 붙인 한 편 안의 자리를 남긴다. 화면이 그 시점으로 바로 옮겨 간다.
        cut = placement.get(shot["id"])
        in_sequence = {
            "file": ctx.rel(previz_dir / sequence["file"]), "seconds": sequence["seconds"],
            "at": cut["at"], "start": cut["start"], "end": cut["end"],
        } if sequence and cut else None
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
                "preset": p["preset"], "sequence": in_sequence,
                "blendFile": ctx.rel(previz_dir / result["blend"]) if result.get("blend") else None,
                # 프레임별 카메라 값은 job.json을 불리므로 파일에 두고 여기서 가리킨다.
                "pathFile": ctx.rel(result_path),
                "files": {name: ([ctx.rel(previz_dir / item) for item in value]
                                 if isinstance(value, list) else
                                 ctx.rel(previz_dir / value) if value else None)
                          for name, value in files.items()},
            },
        )


PREVIZ = Recipe(id="previz", label="프리비즈 샷", prepare=_prepare, run=_run)
