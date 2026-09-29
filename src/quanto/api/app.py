from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from quanto.api.routes import router
from quanto.api.security import security_error, session_token
from quanto.workbench.service import WorkbenchService


def create_app(root: Path, frontend_dir: Path | None = None) -> FastAPI:
    service = WorkbenchService(root)
    token = session_token(service.store.root)

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

    app.include_router(router, prefix="/api")
    if frontend_dir is not None and Path(frontend_dir).is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    return app
