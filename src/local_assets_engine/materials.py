"""Scanned CC0 materials and models, pinned by URL and checksum.

``presets.json`` names each source: ``buildingKit.materials`` (role → texture scan) and ``scanLibrary``
(id → texture or model scan for terrain layers and rocks). Files go to ``models/materials/<id>/`` (not in
Git) with a ``material.json`` that records the source, author, license and real-world size. A texture's
maps are ``diff``·``nor_gl``·``rough``·``ao``·``disp`` ``.jpg``; a model keeps its glTF file names. An
ambientCG scan comes as one zip whose named members are checked one by one. Generation never downloads;
``python -m local_assets_engine.materials fetch`` does, after the license check recorded in docs/DECISIONS.md.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .paths import ROOT
from .presets import load_presets

MATERIALS_DIR = ROOT / "models" / "materials"
USER_AGENT = "local-assets-engine/1.0 (CC0 material fetch)"
RECORD_KEYS = ("source", "kind", "id", "page", "author", "method", "license", "licenseUrl", "sizeMeters",
               "dimensionsMeters", "resolution", "gltf")


def kit_materials(presets: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return presets["buildingKit"]["materials"]


def scan_library(presets: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return presets.get("scanLibrary", {})


def material_dir(source: dict[str, Any]) -> Path:
    return MATERIALS_DIR / source["id"]


def file_path(source: dict[str, Any], name: str) -> Path:
    """Where a pinned file lives. Texture maps are bare roles (``diff`` → ``diff.jpg``)."""
    relative = PurePosixPath(name if PurePosixPath(name).suffix else f"{name}.jpg")
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{source['id']}: 재질 폴더 밖 경로입니다: {name}")
    return material_dir(source).joinpath(*relative.parts)


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sources(presets: dict[str, Any], scans: Iterable[str] | None) -> list[tuple[str, dict[str, Any]]]:
    """(role, source) pairs: the building kit's when ``scans`` is None, else those library scans."""
    if scans is None:
        return list(kit_materials(presets).items())
    library = scan_library(presets)
    return [(scan_id, library[scan_id]) for scan_id in scans]


def missing(presets: dict[str, Any], scans: Iterable[str] | None = None) -> list[str]:
    """Pinned files that are absent or differ from their checksum, as ``id/name``.

    Without ``scans`` this is the building kit's materials; otherwise the named ``scanLibrary`` entries.
    """
    return [item for _role, source in _sources(presets, scans) for item in missing_files(source)]


def missing_files(source: dict[str, Any]) -> list[str]:
    """One pinned source's absent or changed files, as ``id/name``."""
    gone = []
    if not (material_dir(source) / "material.json").is_file():
        gone.append(f"{source['id']}/material.json")
    for name, entry in source["files"].items():
        path = file_path(source, name)
        if not path.is_file() or _md5(path) != entry["md5"]:
            gone.append(f"{source['id']}/{name}")
    return gone


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=300).read()


def _fetch_source(role: str, source: dict[str, Any]) -> list[Path]:
    written = []
    archive = None
    for name, entry in source["files"].items():
        path = file_path(source, name)
        if path.is_file() and _md5(path) == entry["md5"]:
            continue
        if entry.get("member"):
            if archive is None:
                data = _download(source["archive"]["url"])
                if hashlib.md5(data).hexdigest() != source["archive"]["md5"]:
                    raise SystemExit(f"{source['id']}: 받은 압축 파일의 md5가 고정값과 다릅니다.")
                archive = zipfile.ZipFile(io.BytesIO(data))
            # 압축 안의 경로는 쓰지 않고 고정한 이름의 내용만 꺼낸다.
            data = archive.read(entry["member"])
        else:
            data = _download(entry["url"])
        if hashlib.md5(data).hexdigest() != entry["md5"]:
            raise SystemExit(f"{source['id']}/{name}: 받은 파일의 md5가 고정값과 다릅니다.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(path)
    record = {key: source[key] for key in RECORD_KEYS if key in source}
    (material_dir(source) / "material.json").write_text(json.dumps({**record, "role": role}, indent=2), "utf-8")
    return written


def fetch(presets: dict[str, Any], scans: Iterable[str] | None = None) -> list[Path]:
    written = []
    for role, source in _sources(presets, scans):
        material_dir(source).mkdir(parents=True, exist_ok=True)
        written += _fetch_source(role, source)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "check"))
    parser.add_argument("--scan", action="append", help="scanLibrary id만 받는다(여러 번). 없으면 건축 키트와 전체 스캔")
    args = parser.parse_args(argv)
    presets = load_presets()
    groups = [("스캔", args.scan)] if args.scan else [("건축 키트 재질", None), ("스캔", list(scan_library(presets)))]
    failed = False
    for label, scans in groups:
        if args.command == "fetch":
            for path in fetch(presets, scans):
                print(f"받음 {path.relative_to(ROOT)}")
        gone = missing(presets, scans)
        failed |= bool(gone)
        print(f"{label}: " + ("준비됨" if not gone else "없음 " + ", ".join(gone[:6])))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
