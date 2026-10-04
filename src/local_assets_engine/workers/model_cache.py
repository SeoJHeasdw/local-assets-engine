"""Bounded structural checks for local model files, without importing ML runtimes."""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path

DTYPE_BYTES = {"BOOL": 1, "U8": 1, "I8": 1, "F8_E4M3": 1, "F8_E5M2": 1,
               "I16": 2, "U16": 2, "F16": 2, "BF16": 2, "I32": 4, "U32": 4,
               "F32": 4, "F64": 8, "I64": 8, "U64": 8}


def json_file(path: Path):
    """Unreadable/truncated configuration is unavailable, not a successful check."""
    try:
        with Path(path).open(encoding="utf-8") as handle:
            result = json.load(handle)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def valid_safetensors(path: Path) -> bool:
    """Verify the header and complete tensor payload length with bounded IO.

    This detects empty/truncated/malformed files. It does not claim that a file
    whose payload bytes were modified still matches the published SHA-256.
    """
    try:
        size = Path(path).stat().st_size
        with Path(path).open("rb") as handle:
            prefix = handle.read(8)
            if len(prefix) != 8:
                return False
            length = struct.unpack("<Q", prefix)[0]
            if not 2 <= length <= min(16 * 1024 * 1024, size - 8):
                return False
            header = json.loads(handle.read(length))
        if not isinstance(header, dict):
            return False
        intervals = []
        for name, tensor in header.items():
            if name == "__metadata__":
                if not isinstance(tensor, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tensor.items()):
                    return False
                continue
            if not isinstance(tensor, dict) or tensor.get("dtype") not in DTYPE_BYTES:
                return False
            shape, offsets = tensor.get("shape"), tensor.get("data_offsets")
            if not isinstance(shape, list) or any(type(n) is not int or n < 0 for n in shape):
                return False
            if not isinstance(offsets, list) or len(offsets) != 2 or any(type(n) is not int for n in offsets):
                return False
            begin, end = offsets
            if begin < 0 or end < begin or end - begin != math.prod(shape) * DTYPE_BYTES[tensor["dtype"]]:
                return False
            intervals.append((begin, end))
        if not intervals:
            return False
        cursor = 0
        for begin, end in sorted(intervals):
            if begin != cursor:
                return False
            cursor = end
        return cursor == size - 8 - length
    except (OSError, ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        return False


def safe_relative_file(folder: Path, filename: str) -> Path | None:
    # HF snapshot files legitimately symlink to content-addressed cache blobs;
    # validate the declared relative name, not their physical blob location.
    if not isinstance(filename, str):
        return None
    relative = Path(filename)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return None
    return Path(folder) / relative


def weight_files_missing(folder: Path) -> list[str]:
    folder = Path(folder)
    indexes = sorted(folder.glob("*.safetensors.index.json"))
    if indexes:
        index = json_file(indexes[0])
        mapping = index.get("weight_map") if index else None
        if not isinstance(mapping, dict) or not mapping or not all(isinstance(v, str) for v in mapping.values()):
            return [indexes[0].name]
        files = sorted(set(mapping.values()))
    else:
        files = [path.name for path in sorted(folder.glob("*.safetensors"))]
    if not files:
        return ["*.safetensors"]
    return [name for name in files if (path := safe_relative_file(folder, name)) is None or not valid_safetensors(path)]
