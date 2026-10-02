from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from quanto.api.routes import router
from quanto.api.security import security_error, session_token
from quanto.workbench.service import WorkbenchService
from quanto.workbench.revisions import digest, json_bytes


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        path = path.replace("\\", "/")
        if path == "api" or path.startswith("api/"):
            raise StarletteHTTPException(404)
        # Only known client routes fall back; missing assets/private paths stay 404.
        is_page = path in {".", "", "datasets", "experiments", "runs", "compare", "learning"} or (path.startswith("runs/") and len(path.split("/")) == 2)
        if is_page and scope["method"] in {"GET", "HEAD"}:
            return FileResponse(Path(self.directory) / "index.html", headers={"Cache-Control": "no-cache"})
        return await super().get_response(path, scope)


def create_app(root: Path, frontend_dir: Path | None = None, *, templates_dir: Path | None = None) -> FastAPI:
    service = WorkbenchService(root)
    token = session_token(service.store.root)
    if templates_dir is not None:
        with service.store.transaction() as db:
            existing = {row[0] for row in db.execute("SELECT content_hash FROM strategies")}
        for name in ("buy_hold", "trend_rotation"):
            directory = templates_dir / name
            manifest = json.loads((directory / "strategy.json").read_text(encoding="utf-8"))
            names = ["strategy.json", manifest["entrypoint"].partition(":")[0], *manifest["auxiliary_sources"]]
            files = {filename: (directory / filename).read_bytes() for filename in names}
            content_hash = digest(json_bytes({filename: digest(data) for filename, data in files.items()}))
            if content_hash not in existing:
                service.store.register_strategy(files, manifest)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.scheduler.start()
        try:
            yield
        finally:
            service.scheduler.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.service = service
    app.state.token = token

    @app.middleware("http")
    async def secure(request: Request, call_next):
        denied = security_error(request, token)
        if denied is not None:
            return denied
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError):
        return JSONResponse({"code": "NOT_FOUND", "message": "记录不存在", "details": {}}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        message = str(exc)
        if "conflict" in message:
            return JSONResponse({"code": "CONFLICT", "message": "修订或幂等键冲突", "details": {}}, status_code=409)
        return JSONResponse({"code": "INVALID_ARGUMENT", "message": "请求参数无效", "details": {"reason": message}}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, exc: RequestValidationError):
        errors = [{"path": list(error["loc"]), "type": error["type"]} for error in exc.errors()]
        return JSONResponse({"code": "INVALID_ARGUMENT", "message": "请求参数无效", "details": {"errors": errors}}, status_code=422)

    @app.exception_handler(SyntaxError)
    async def syntax(request: Request, exc: SyntaxError):
        return JSONResponse({"code": "SYNTAX_ERROR", "message": "策略代码语法有误",
                             "details": {"file": exc.filename, "line": exc.lineno, "offset": exc.offset}}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        message = "接口不存在" if exc.status_code == 404 else "请求无法完成"
        return JSONResponse({"code": code, "message": message, "details": {}}, status_code=exc.status_code)

    from quanto.learning.api import router as learning_router
    app.include_router(learning_router, prefix="/api")
    app.include_router(router, prefix="/api")
    if frontend_dir is not None:
        app.mount("/", FrontendFiles(directory=frontend_dir), name="frontend")
    return app
