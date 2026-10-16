"""Scanned CC0 materials the building kit is cut from, pinned by URL and checksum.

``presets.json`` → ``buildingKit.materials`` names each source (role → files with md5). Files go to
``models/materials/<id>/`` (not in Git) with a ``material.json`` that records the source, license
and real-world size. Generation never downloads; ``python -m local_assets_engine.materials fetch``
does, after the license check recorded in docs/DECISIONS.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any

from .paths import ROOT
from .presets import load_presets

MATERIALS_DIR = ROOT / "models" / "materials"
USER_AGENT = "local-assets-engine/1.0 (CC0 material fetch)"


def kit_materials(presets: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return presets["buildingKit"]["materials"]


def material_dir(source: dict[str, Any]) -> Path:
    return MATERIALS_DIR / source["id"]


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def missing(presets: dict[str, Any]) -> list[str]:
    """Material files that are absent or differ from their pinned checksum, as ``id/name``."""
    gone = []
    for source in kit_materials(presets).values():
        folder = material_dir(source)
        if not (folder / "material.json").is_file():
            gone.append(f"{source['id']}/material.json")
        for name, entry in source["files"].items():
            path = folder / f"{name}.jpg"
            if not path.is_file() or _md5(path) != entry["md5"]:
                gone.append(f"{source['id']}/{name}")
    return gone


def fetch(presets: dict[str, Any]) -> list[Path]:
    written = []
    for role, source in kit_materials(presets).items():
        folder = material_dir(source)
        folder.mkdir(parents=True, exist_ok=True)
        for name, entry in source["files"].items():
            path = folder / f"{name}.jpg"
            if path.is_file() and _md5(path) == entry["md5"]:
                continue
            request = urllib.request.Request(entry["url"], headers={"User-Agent": USER_AGENT})
            data = urllib.request.urlopen(request, timeout=180).read()
            if hashlib.md5(data).hexdigest() != entry["md5"]:
                raise SystemExit(f"{source['id']}/{name}: 받은 파일의 md5가 고정값과 다릅니다.")
            path.write_bytes(data)
            written.append(path)
        record = {key: source[key] for key in ("source", "id", "page", "license", "licenseUrl", "sizeMeters")}
        (folder / "material.json").write_text(json.dumps({**record, "role": role}, indent=2), "utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "check"))
    args = parser.parse_args(argv)
    presets = load_presets()
    if args.command == "fetch":
        for path in fetch(presets):
            print(f"받음 {path.relative_to(ROOT)}")
    gone = missing(presets)
    print("건축 키트 재질: " + ("준비됨" if not gone else "없음 " + ", ".join(gone)))
    return 0 if not gone else 1


if __name__ == "__main__":
    raise SystemExit(main())
