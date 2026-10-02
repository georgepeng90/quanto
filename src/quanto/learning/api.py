from __future__ import annotations

import tempfile
from pathlib import Path
import zipfile

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from quanto.learning.service import LearningService


router = APIRouter(prefix='/learning')


def service(request):
    return LearningService(request.app.state.service)


@router.get('/overview')
def overview(request: Request):
    return service(request).overview()


@router.post('/datasets')
def prepare(request: Request, body: dict):
    return service(request).prepare(body)


@router.post('/train')
def train(request: Request, body: dict):
    return service(request).train(body)


@router.post('/backtests')
def backtest(request: Request, body: dict):
    return service(request).backtest(body)


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
    learning = service(request)
    ref = learning.reference(identity, 'backtest')
    root = Path(ref['path'])
    with tempfile.NamedTemporaryFile(suffix='.zip', dir=learning.store.artifact_root, delete=False) as file:
        path = Path(file.name)
    try:
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
            for name in ref['files']:
                if name != 'results.sqlite3':
                    bundle.write(root / name, name)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return FileResponse(path, filename=f'learning-backtest-{identity}.zip', background=BackgroundTask(path.unlink, missing_ok=True))


@router.get('/{resource}')
def listing(request: Request, resource: str, cursor: str | None = None, limit: int = 50):
    return service(request).listing(resource, cursor, limit)


@router.get('/{resource}/{identity}')
def detail(request: Request, resource: str, identity: str):
    return service(request).detail(resource, identity)
