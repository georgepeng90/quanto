from __future__ import annotations

from pathlib import Path
import tempfile
import zipfile

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from quanto.investing.service import InvestingService


router = APIRouter(prefix='/investing')


def service(request):
    return InvestingService(request.app.state.service)


def models(request):
    from quanto.investing.models import InvestingModels
    return InvestingModels(request.app.state.service)


@router.get('/models')
def list_models(request: Request, cursor: str | None = None, limit: int = 50):
    return models(request).list_models(cursor, limit)


@router.post('/models')
def create_model(request: Request, body: dict):
    return models(request).create_model(body)


@router.get('/models/{identity}')
def get_model(request: Request, identity: str, include_history: bool = True):
    return models(request).get_model(identity, include_history)


@router.get('/models/{identity}/history')
def model_history(request: Request, identity: str, kind: str = 'versions', page: int = 1,
                  page_size: int = 10, anchor: int | None = None,
                  round_number: int | None = None, session_number: int | None = None):
    return models(request).history(identity, kind, page, page_size, anchor, round_number, session_number)


@router.post('/models/{identity}/train')
def train_model(request: Request, identity: str, body: dict):
    return models(request).train_model(identity, body)


@router.post('/models/{identity}/backtests')
def backtest_model(request: Request, identity: str, body: dict):
    return models(request).backtest_model(identity, body)


@router.get('/overview')
def overview(request: Request):
    return service(request).overview()


@router.post('/train')
def train(request: Request, body: dict):
    return JSONResponse({"code": "MIGRATION_REQUIRED", "message": "Create a framework model and train it through its model ID",
                         "details": {}}, status_code=422)


@router.post('/backtests')
def backtest(request: Request, body: dict):
    return JSONResponse({"code": "MIGRATION_REQUIRED", "message": "Backtest a saved framework model version through its model ID",
                         "details": {}}, status_code=422)


@router.get('/backtests/{identity}/tables/{name}')
def table(request: Request, identity: str, name: str, cursor: str | None = None, limit: int = 100):
    from quanto.learning.results import table as query_table
    return query_table(service(request).result_root(identity), name, cursor, limit)


@router.get('/backtests/{identity}/charts')
def charts(request: Request, identity: str, max_points: int = 1000):
    from quanto.learning.results import chart
    return chart(service(request).result_root(identity), max_points)


@router.get('/backtests/{identity}/export')
def export(request: Request, identity: str):
    investing = service(request)
    ref = investing.reference(identity, 'policy_backtest')
    root = Path(ref['path'])
    with tempfile.NamedTemporaryFile(suffix='.zip', dir=investing.store.artifact_root, delete=False) as file:
        path = Path(file.name)
    try:
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
            for name in ref['files']:
                if name != 'results.sqlite3':
                    bundle.write(root / name, name)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return FileResponse(path, filename=f'etf-policy-backtest-{identity}.zip', background=BackgroundTask(path.unlink, missing_ok=True))


@router.get('/{resource}')
def listing(request: Request, resource: str, cursor: str | None = None, limit: int = 50):
    return service(request).listing(resource, cursor, limit)


@router.get('/{resource}/{identity}')
def detail(request: Request, resource: str, identity: str):
    return service(request).detail(resource, identity)
