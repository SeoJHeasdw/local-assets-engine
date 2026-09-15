"""Locations of code, external engines, tools and outputs."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PRESETS_PATH = CONFIG_DIR / "presets.json"
ENGINES_DIR = ROOT / "engines"
TRELLIS_DIR = ENGINES_DIR / "trellis-mac"
RENDERER_DIR = ROOT / "electron-app" / "renderer"
SHARED_DIR = ROOT / "electron-app" / "shared"
MODEL_VIEWER_JS = ROOT / "node_modules" / "@google" / "model-viewer" / "dist" / "model-viewer.min.js"

# GUI 앱(Electron)에서 띄운 프로세스는 셸 PATH를 물려받지 못한다.
# Homebrew와 npm 전역 설치 위치를 명시적으로 덧붙인다.
EXTRA_PATHS = ("/opt/homebrew/bin", "/usr/local/bin")


def output_dir() -> Path:
    return Path(os.environ.get("LOCAL_ASSETS_OUTPUT", ROOT / "output"))


def jobs_dir() -> Path:
    return output_dir() / "jobs"


def engine_bin(name: str) -> Path:
    """Executable installed in the engine's own virtual environment."""
    return Path(sys.executable).parent / name


def trellis_python() -> Path:
    return TRELLIS_DIR / ".venv" / "bin" / "python"


def trellis_generate_script() -> Path:
    return TRELLIS_DIR / "generate.py"


def find_tool(name: str) -> Path | None:
    found = shutil.which(name, path=tool_path())
    return Path(found) if found else None


def tool_path() -> str:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join([*parts, *[p for p in EXTRA_PATHS if p not in parts]])


def child_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = tool_path()
    env["PYTHONUNBUFFERED"] = "1"
    env.update(overrides)
    return env
