from fastapi import APIRouter, Request
from fastapi.responses import Response

from quanto.screening.contracts import Key, ScreeningRequest
from quanto.framework import FrozenContract
from quanto.screening.store import ScreeningStore

router = APIRouter()


def _store(request):
    return ScreeningStore(request.app.state.service.store)


@router.post("/screenings")
def submit(request: Request, body: ScreeningRequest):
    return _store(request).submit(body).model_dump(mode="json")


@router.get("/screenings")
def listing(request: Request, cursor: str | None = None, limit: int = 50):
    return _store(request).list(cursor, limit)


@router.get("/screenings/{identity}")
def detail(request: Request, identity: str):
    return _store(request).get(identity).model_dump(mode="json")


@router.get("/screenings/{identity}/export")
def export(request: Request, identity: str):
    return Response(_store(request).export(identity), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=screening.json"})


class BasketHandoff(FrozenContract):
    name: Key
    idempotency_key: Key


@router.post("/screenings/{identity}/basket")
def use(request: Request, identity: str, body: BasketHandoff):
    return _store(request).create_basket(identity, body.name, body.idempotency_key).model_dump(mode="json")
