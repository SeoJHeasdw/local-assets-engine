"""Contract self-test recipe: the job lane without any model.

arkAgent's engine check drives only this recipe. Each step is a measured stage
process like any generation stage, so queueing, progress, cancellation,
measurement and failure records take the same paths a real job takes.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from .base import Recipe, float_param, int_param

if TYPE_CHECKING:
    from ..jobs import JobStore
    from ..runner import JobContext

MAX_STEPS = 10
MAX_STEP_SECONDS = 30.0


def _prepare(params: dict[str, Any], _presets: dict[str, Any], _store: "JobStore") -> tuple[dict[str, Any], str]:
    steps = int_param(params, "steps", 2, 1, MAX_STEPS)
    normalized: dict[str, Any] = {
        "steps": steps,
        "stepSeconds": float_param(params, "stepSeconds", 0.5, 0.0, MAX_STEP_SECONDS),
    }
    if params.get("failAt") is not None:
        normalized["failAt"] = int_param(params, "failAt", 1, 1, steps)
    return normalized, "엔진 자체 점검"


def _run(ctx: "JobContext") -> None:
    p = ctx.params
    for step in range(1, p["steps"] + 1):
        with ctx.stage(f"step-{step}", f"점검 {step}단계") as stage:
            args = [sys.executable, "-m", "local_assets_engine.tools.selftest_step", "--seconds", p["stepSeconds"]]
            if p.get("failAt") == step:
                args.append("--fail")
            stage.run(args, cwd=ctx.dir)
    report = ctx.dir / "selftest" / "report.txt"
    report.parent.mkdir(exist_ok=True)
    report.write_text(f"selftest: {p['steps']}단계 · 단계당 {p['stepSeconds']}초\n", "utf-8")
    asset = ctx.add_asset(kind="text", role="final", file=report)
    # 계약 검사가 findings의 모양을 볼 수 있게 남기는 표본이다. 실제 검사 결과가 아니다.
    ctx.add_finding(severity="warn", code="selftest.sample", params={"step": 1}, where="1단계",
                    asset_id=asset["id"], evidence=[report])


SELFTEST = Recipe(id="selftest", label="엔진 자체 점검", prepare=_prepare, run=_run,
                  needs_review=False, diagnostic=True)
