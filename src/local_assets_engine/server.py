"""Local HTTP API and app screen, bound to 127.0.0.1.

The Electron app loads its screen from here, and javis or scripts can call the
same API. One runner lane serves every client.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, doctor
from .bench import bench_rows
from .jobs import REVIEW_STATES, JobNotFound, JobStore, now_iso
from .paths import MODEL_VIEWER_JS, RENDERER_DIR, SHARED_DIR, jobs_dir, output_dir
from .presets import PresetError, load_presets
from .runner import Runner

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


def _hostname(value: str) -> str:
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def create_app(*, store: JobStore | None = None, runner: Runner | None = None, start_runner: bool = True) -> FastAPI:
    store = store or JobStore(jobs_dir())
    runner = runner or Runner(store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_runner:
            runner.start()
        yield
        if start_runner:
            runner.shutdown()

    app = FastAPI(title="Local Assets Engine", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.state.runner = runner

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # 다른 호스트 이름으로 들어온 요청(DNS 리바인딩)과 다른 사이트에서 보낸 요청을 막는다.
        host = _hostname(request.headers.get("host", ""))
        origin = request.headers.get("origin")
        if host not in LOCAL_HOSTS:
            return JSONResponse({"detail": "로컬 요청만 받습니다."}, status_code=403)
        if origin and origin != "null" and _hostname(origin.split("://", 1)[-1]) not in LOCAL_HOSTS:
            return JSONResponse({"detail": "다른 출처의 요청은 받지 않습니다."}, status_code=403)
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        # CLI가 돌리는 작업도 화면에 보여야 한다. 이 엔진이 시작하지 않은 작업이라도
        # 프로세스가 살아 있으면 진행 중으로 읽는다.
        return {"ok": True, "version": __version__,
                "currentJob": runner.current_job_id or store.running_job_id(),
                "outputDir": str(output_dir())}

    @app.get("/api/doctor")
    def get_doctor(deep: bool = False) -> dict[str, Any]:
        return doctor.build_report(deep=deep)

    @app.get("/api/presets")
    def get_presets() -> dict[str, Any]:
        return load_presets()

    @app.get("/api/bench")
    def get_bench() -> dict[str, Any]:
        return {"rows": bench_rows(store)}

    @app.get("/api/jobs")
    def list_jobs(limit: int = 100) -> dict[str, Any]:
        return {"jobs": store.list(limit=max(1, min(limit, 500)))}

    @app.post("/api/jobs", status_code=201)
    def create_job(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return runner.submit(str(payload.get("recipe") or ""), payload.get("params") or {})
        except PresetError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        try:
            return store.load(job_id)
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.") from error

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            return runner.cancel(job_id)
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.") from error

    @app.post("/api/jobs/{job_id}/assets/{asset_id}/review")
    def review_asset(job_id: str, asset_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        status = payload.get("status")
        if status not in REVIEW_STATES:
            raise HTTPException(status_code=400, detail=f"status는 {sorted(REVIEW_STATES)} 중 하나여야 합니다.")

        def apply(job: dict[str, Any]) -> None:
            asset = next((item for item in job["assets"] if item["id"] == asset_id), None)
            if asset is None:
                raise JobNotFound(asset_id)
            asset.update(review=status, reviewedAt=now_iso())

        try:
            return store.update(job_id, apply)
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail="에셋을 찾을 수 없습니다.") from error

    @app.get("/api/assets")
    def list_assets(review: str | None = None, kind: str | None = None, limit: int = 500) -> dict[str, Any]:
        assets = []
        for job in store.list(limit=2000):
            for asset in job["assets"]:
                if (review and asset["review"] != review) or (kind and asset["kind"] != kind):
                    continue
                assets.append({**asset, "jobId": job["id"], "jobTitle": job["title"], "recipe": job["recipe"]})
        return {"assets": assets[: max(1, min(limit, 2000))]}

    @app.get("/files/{job_id}/{relative:path}")
    def job_file(job_id: str, relative: str) -> FileResponse:
        try:
            return FileResponse(store.resolve_file(job_id, relative))
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.") from error

    @app.get("/vendor/model-viewer.min.js")
    def model_viewer() -> FileResponse:
        if not MODEL_VIEWER_JS.exists():
            raise HTTPException(status_code=404, detail="npm install 후 3D 미리보기를 쓸 수 있습니다.")
        return FileResponse(MODEL_VIEWER_JS, media_type="text/javascript")

    # 화면의 `../shared/x.mjs` import는 URL 루트에서 `/shared/x.mjs`가 된다.
    if SHARED_DIR.is_dir():
        app.mount("/shared", StaticFiles(directory=SHARED_DIR), name="shared")
    if RENDERER_DIR.is_dir():
        app.mount("/", StaticFiles(directory=RENDERER_DIR, html=True), name="renderer")
    return app


def serve(port: int) -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
