"""Organizing assets across jobs: favorites and collections, version lineage, disk use.

Nothing here changes generated files. Library fields live on the asset record in
``job.json``; storage reports only classify files so a person can decide what to keep.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from .presets import PresetError

MAX_TAGS = 20
RECORD_FILES = frozenset({"job.json", "job.log", ".record.lock", "job.json.tmp"})
# 다음 작업이 예전 작업 폴더에서 다시 읽는 파일. 에셋 기록에 경로가 없어도 지우면 안 된다.
BASE_FILES = ("mesh/input.png", "mesh/source.json", "mesh/source.npz", "mesh/audit/decoded.npz")
# 생성이 끝난 뒤 어떤 레시피·화면도 읽지 않는 계산 중간물. 사람이 휴지통으로 보낼 수 있다.
INTERMEDIATE_PATTERNS = ("*/surface/*", "surface/*", "*/lod-work/*", "*/scratch/*", "scratch/*", "*/audit/*")
CATEGORY_LABELS = {
    "results": "결과물", "bases": "다시 만들기용 원본", "copies": "전송·비교용 사본",
    "previews": "미리보기·검수", "intermediate": "중간 파일", "records": "작업 기록", "other": "기타",
}
RELATION_LABELS = {
    "edit-asset": "편집", "refine-mesh": "원본 재구성", "repair-mesh": "복구",
    "image-to-3d": "3D 변환", "text-to-3d": "3D 변환",
}


def update_library(asset: dict[str, Any], payload: dict[str, Any]) -> None:
    """Apply favorite/tags/collection/note from an API payload after validating it."""
    if not isinstance(payload, dict):
        raise PresetError("정리할 내용을 보내 주세요.")
    tag_ops = [name for name in ("tags", "addTags", "removeTags") if name in payload]
    if len(tag_ops) > 1:
        raise PresetError("태그 추가·제거·교체 중 하나만 요청해 주세요.")
    if tag_ops and tag_ops[0] != "tags":
        operation = tag_ops[0]
        checked: dict[str, Any] = {}
        update_library(checked, {"tags": payload[operation]})
        changed = checked.get("tags", [])
        current = asset.get("tags", [])
        tags = current + changed if operation == "addTags" else [tag for tag in current if tag not in changed]
        payload = {**payload, "tags": tags}
    if "favorite" in payload:
        if not isinstance(payload["favorite"], bool):
            raise PresetError("favorite는 true 또는 false여야 합니다.")
        if payload["favorite"]:
            asset["favorite"] = True
        else:
            asset.pop("favorite", None)
    if "tags" in payload:
        tags = payload["tags"]
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise PresetError("태그는 글자 목록이어야 합니다.")
        cleaned: list[str] = []
        for tag in tags:
            tag = " ".join(tag.split())[:40]
            if tag and tag not in cleaned:
                cleaned.append(tag)
        if len(cleaned) > MAX_TAGS:
            raise PresetError(f"태그는 {MAX_TAGS}개까지 붙일 수 있습니다.")
        if cleaned:
            asset["tags"] = cleaned
        else:
            asset.pop("tags", None)
    for key, limit in (("collection", 60), ("note", 500)):
        if key not in payload:
            continue
        value = payload[key]
        if value is not None and not isinstance(value, str):
            raise PresetError(f"{key}는 글자여야 합니다.")
        value = " ".join((value or "").split())[:limit] if key == "collection" else (value or "").strip()[:limit]
        if value:
            asset[key] = value
        else:
            asset.pop(key, None)


def _key(job_id: str, asset_id: str) -> str:
    return f"{job_id}/{asset_id}"


def _edit_summary(plan: dict[str, Any] | None) -> list[str]:
    if not isinstance(plan, dict):
        return []
    layers = plan.get("layers")
    if layers is None:
        layers = []
        if (plan.get("recolor") or {}).get("enabled"):
            layers.append({"type": "color"})
        if (plan.get("overlay") or {}).get("enabled"):
            layers.append({"type": "stamp", "text": plan["overlay"].get("text")})
    visible = [layer for layer in layers if layer.get("visible", True)]
    parts = []
    colors = sum(layer.get("type") == "color" for layer in visible)
    texts = [layer.get("text") for layer in visible if layer.get("type") == "stamp" and layer.get("text")]
    logos = sum(layer.get("type") == "stamp" and not layer.get("text") for layer in visible)
    if colors:
        parts.append(f"색 {colors}")
    if texts:
        parts.append("문구 " + ", ".join(texts[:2]))
    if logos:
        parts.append(f"로고 {logos}")
    if any(plan.get(key, 1) != 1 for key in ("brightness", "contrast", "saturation")):
        parts.append("톤 보정")
    frame = plan.get("frame") or {}
    if frame.get("turns") or frame.get("flipX") or frame.get("crop") not in (None, "original") or frame.get("padding") or frame.get("width"):
        parts.append("구성")
    return parts


def lineage(jobs: list[dict[str, Any]], job_id: str, asset_id: str) -> dict[str, Any]:
    """Version tree around one image/mesh: its root and every version derived from it."""
    nodes: dict[str, dict[str, Any]] = {}
    for job in jobs:
        for asset in job["assets"]:
            if asset["kind"] not in ("image", "mesh"):
                continue
            meta = asset.get("meta") or {}
            source = meta.get("source") if isinstance(meta.get("source"), dict) else None
            parent = _key(str(source.get("jobId")), str(source.get("assetId"))) if source else None
            if not parent and meta.get("conceptAsset") and meta["conceptAsset"] != asset["id"]:
                parent = _key(job["id"], meta["conceptAsset"])
            nodes[_key(job["id"], asset["id"])] = {
                "jobId": job["id"], "assetId": asset["id"], "kind": asset["kind"], "role": asset.get("role"),
                "title": (job.get("params") or {}).get("subject") or job["title"], "label": meta.get("label"),
                "preview": asset.get("preview"), "file": asset["file"], "createdAt": asset.get("createdAt") or job.get("createdAt"),
                "recipe": job["recipe"], "relation": RELATION_LABELS.get(job["recipe"]) if parent else None,
                "summary": _edit_summary(meta.get("editPlan")) if job["recipe"] == "edit-asset" else [],
                "favorite": bool(asset.get("favorite")), "parent": parent,
            }
    current = _key(job_id, asset_id)
    if current not in nodes:
        raise KeyError(current)
    root, seen = current, {current}
    while nodes[root]["parent"] in nodes and nodes[root]["parent"] not in seen:
        root = nodes[root]["parent"]
        seen.add(root)
    children: dict[str, list[str]] = {}
    for key, node in nodes.items():
        if node["parent"] in nodes:
            children.setdefault(node["parent"], []).append(key)
    ordered: list[dict[str, Any]] = []

    def walk(key: str, depth: int, trail: set[str]) -> None:
        node = {**nodes[key], "key": key, "depth": depth,
                "missingParent": depth == 0 and bool(nodes[key]["parent"]) and nodes[key]["parent"] not in nodes}
        ordered.append(node)
        for child in sorted(children.get(key, []), key=lambda k: nodes[k]["createdAt"] or ""):
            if child not in trail:
                walk(child, depth + 1, trail | {child})

    walk(root, 0, {root})
    return {"current": current, "root": root, "versions": ordered}


def _meta_files(value: Any, key: str = "") -> list[tuple[str, str]]:
    """(가장 가까운 필드 이름, 문자열) 쌍. editStampFiles 안쪽은 레이어 id라 바깥 이름을 쓴다."""
    if isinstance(value, dict):
        return [item for k, v in value.items() if k != "editPlan"
                for item in _meta_files(v, key if key == "editStampFiles" else k)]
    if isinstance(value, list):
        return [item for v in value for item in _meta_files(v, key)]
    return [(key, value)] if isinstance(value, str) else []


def classify_job_files(job_dir: Path, job: dict[str, Any]) -> list[dict[str, Any]]:
    """Every file in a job folder with its size and why it is kept."""
    known: dict[str, str] = {}
    for asset in job.get("assets", []):
        known.setdefault(asset.get("file") or "", "results")
        if asset.get("preview"):
            known.setdefault(asset["preview"], "previews")
        for key, value in _meta_files(asset.get("meta") or {}):
            category = ("bases" if key in ("sourceStateFile", "editBaseFile", "editStampFile", "editStampFiles")
                        else "copies" if key in ("rawFile", "optimizedFile")
                        else "previews" if key == "inspectionFile" else "results")
            known.setdefault(value, category)
    files = []
    for path in sorted(job_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(job_dir).as_posix()
        if relative in known:
            category = known[relative]
        elif (relative in RECORD_FILES or relative in ("request.json", "stats.json", "edit/request.json", "edit/stats.json")
              or relative.endswith((".stats.json", ".bake.json"))):
            category = "records"
        elif relative in BASE_FILES or relative.startswith("edit/logo") or relative.startswith("edit/source."):
            category = "bases"
        elif "/inspection/" in f"/{relative}":
            category = "previews"
        elif any(fnmatch.fnmatch(relative, pattern) for pattern in INTERMEDIATE_PATTERNS):
            category = "intermediate"
        else:
            category = "other"
        files.append({"path": relative, "bytes": path.stat().st_size, "category": category})
    return files


def job_storage(job_dir: Path, job: dict[str, Any]) -> dict[str, Any]:
    files = classify_job_files(job_dir, job)
    categories: dict[str, int] = {}
    for item in files:
        categories[item["category"]] = categories.get(item["category"], 0) + item["bytes"]
    return {"jobId": job["id"], "title": job["title"], "recipe": job["recipe"], "state": job["state"],
            "createdAt": job.get("createdAt"), "bytes": sum(categories.values()), "categories": categories, "files": files}


def storage_report(store, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [job_storage(store.job_dir(job["id"]), job) for job in jobs]
    totals: dict[str, int] = {}
    for row in rows:
        for category, size in row["categories"].items():
            totals[category] = totals.get(category, 0) + size
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    # 다른 작업이 원본으로 삼는 작업. 통째로 지우면 그 작업의 '이전 버전'과 재구성이 끊긴다.
    referenced: dict[str, list[str]] = {}
    for job in jobs:
        for asset in job["assets"]:
            source = (asset.get("meta") or {}).get("source")
            if isinstance(source, dict) and source.get("jobId") and source.get("jobId") != job["id"]:
                referenced.setdefault(str(source["jobId"]), []).append(job["id"])
    for row in rows:
        row["usedBy"] = sorted(set(referenced.get(row["jobId"], [])))
        row["intermediate"] = [item["path"] for item in row.pop("files") if item["category"] == "intermediate"]
    return {"bytes": sum(totals.values()), "categories": totals, "labels": CATEGORY_LABELS, "jobs": rows}
