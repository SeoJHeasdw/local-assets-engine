"""Command line: serve, doctor, run, bench."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_PORT = 47831


def _port() -> int:
    return int(os.environ.get("LOCAL_ASSETS_PORT", DEFAULT_PORT))


def _api(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 5) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{_port()}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _server_running() -> bool:
    try:
        return bool(_api("GET", "/api/health", timeout=1).get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _print_progress(load_job) -> dict[str, Any]:
    seen: dict[tuple[int, str], str] = {}
    while True:
        job = load_job()
        for index, stage in enumerate(job["stages"]):
            key = (index, stage["name"])
            text = f"{stage['state']} {round(stage['progress'] * 100)}% {stage.get('detail') or ''}".strip()
            if seen.get(key) != text:
                seen[key] = text
                print(f"[{stage['label']}] {text}", flush=True)
        if job["state"] not in ("queued", "running", "cancelling"):
            return job
        time.sleep(1)


def _run(recipe: str, raw_params: str) -> int:
    params = json.loads(Path(raw_params[1:]).read_text("utf-8") if raw_params.startswith("@") else raw_params)
    if _server_running():
        # 서버가 떠 있으면 같은 줄에 세운다. 두 곳에서 동시에 모델을 올리면 메모리가 넘친다.
        job = _api("POST", "/api/jobs", {"recipe": recipe, "params": params})
        job_id = job["id"]
        print(f"엔진 서버에 작업을 넣었습니다: {job_id}")
        try:
            job = _print_progress(lambda: _api("GET", f"/api/jobs/{job_id}"))
        except (urllib.error.URLError, OSError) as error:
            # 앱을 닫으면 그 앱이 띄운 엔진도 함께 사라진다. 역추적 대신 어디를 볼지 알린다.
            print(f"엔진 서버와 연결이 끊겼습니다: {error}")
            print(f"진행 상황은 output/jobs/{job_id}/job.json에 남아 있습니다.")
            return 1
    else:
        from .jobs import JobStore
        from .paths import jobs_dir
        from .runner import Runner

        store = JobStore(jobs_dir())
        runner = Runner(store)
        job = runner.create(recipe, params)
        print(f"작업을 시작합니다: {job['id']} ({store.job_dir(job['id'])})")
        worker = threading.Thread(target=runner.run_job, args=(job["id"],), daemon=True)
        worker.start()
        try:
            job = _print_progress(lambda: store.load(job["id"]))
        except KeyboardInterrupt:
            runner.cancel(job["id"])
            worker.join()
            job = store.load(job["id"])
    print(f"결과: {job['state']}" + (f" · {job['error']}" if job.get("error") else ""))
    for asset in job["assets"]:
        print(f"- {asset['id']} {asset['kind']} {asset['file']}")
    return 0 if job["state"] == "done" else 1


def _bench(as_json: bool) -> int:
    from .bench import bench_rows
    from .jobs import JobStore
    from .paths import jobs_dir

    rows = bench_rows(JobStore(jobs_dir()))
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("아직 완료된 단계 기록이 없습니다.")
        return 0
    print(f"{'단계':<10} {'조건':<22} {'횟수':>4} {'중앙값(s)':>10} {'최대(s)':>8} {'최대 메모리(GB)':>15}")
    for row in rows:
        peak = "-" if row["maxPeakMemoryBytes"] is None else f"{row['maxPeakMemoryBytes'] / 2**30:.2f}"
        print(f"{row['stage']:<10} {row['variant']:<22} {row['runs']:>4} "
              f"{row['medianSeconds']:>10} {row['maxSeconds']:>8} {peak:>15}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .recipes import RECIPES

    parser = argparse.ArgumentParser(prog="local-assets", description="로컬 2D/3D 게임 에셋 엔진")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="API와 앱 화면을 127.0.0.1에서 제공")
    serve.add_argument("--port", type=int, default=_port())
    doctor_cmd = commands.add_parser("doctor", help="환경 진단")
    doctor_cmd.add_argument("--json", action="store_true")
    doctor_cmd.add_argument("--strict", action="store_true", help="준비 안 된 기능이 하나라도 있으면 실패")
    doctor_cmd.add_argument("--quick", action="store_true", help="하위 프로세스 검사 생략")
    run = commands.add_parser("run", help="작업 하나를 실행 (서버가 떠 있으면 서버에 넣음)")
    run.add_argument("recipe", choices=sorted(RECIPES))
    run.add_argument("--params", required=True, help="JSON 문자열 또는 @파일경로")
    bench = commands.add_parser("bench", help="단계별 소요 시간·최대 메모리 요약")
    bench.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .server import serve as serve_app

        serve_app(args.port)
        return 0
    if args.command == "doctor":
        from . import doctor

        report = doctor.build_report(deep=not args.quick)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            doctor.print_report(report)
        ready = report["capabilities"].values()
        return 1 if (args.strict and not all(ready)) or not any(ready) else 0
    if args.command == "run":
        return _run(args.recipe, args.params)
    return _bench(args.json)


if __name__ == "__main__":
    sys.exit(main())
