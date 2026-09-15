"""Recipe contract and parameter validation helpers."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from ..presets import PresetError

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

MAX_SEED = 2**31 - 1024


@dataclass(frozen=True)
class Recipe:
    id: str
    label: str
    prepare: Callable[[dict[str, Any], dict[str, Any], "JobStore"], tuple[dict[str, Any], str]]
    run: Callable[["JobContext"], None]


def int_param(params: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = params.get(key, default)
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise PresetError(f"{key}는 정수여야 합니다.") from error
    if not low <= number <= high:
        raise PresetError(f"{key}는 {low}~{high} 사이여야 합니다.")
    return number


def float_param(params: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = params.get(key, default)
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise PresetError(f"{key}는 숫자여야 합니다.") from error
    if not low <= number <= high:
        raise PresetError(f"{key}는 {low}~{high} 사이여야 합니다.")
    return number


def choice_param(params: dict[str, Any], key: str, default: Any, choices: list[Any]) -> Any:
    value = params.get(key, default)
    for choice in choices:
        if str(choice) == str(value):
            return choice
    raise PresetError(f"{key}는 {choices} 중 하나여야 합니다.")


def bool_param(params: dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on", "0", "false", "no", "off"):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value in (0, 1):
        return bool(value)
    raise PresetError(f"{key}는 true 또는 false여야 합니다.")


def seed_param(params: dict[str, Any], count: int = 1) -> int:
    value = params.get("seed")
    if value in (None, ""):
        return secrets.randbelow(MAX_SEED - count)
    return int_param(params, "seed", 0, 0, MAX_SEED - count)


def dimension_param(params: dict[str, Any], key: str, default: int) -> int:
    value = int_param(params, key, default, 256, 2048)
    return value - value % 16
