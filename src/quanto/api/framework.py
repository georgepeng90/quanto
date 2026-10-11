"""Narrow public routes for immutable framework resources."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import TypeAdapter

from quanto.framework import BasketRevisionRef
from quanto.workbench.baskets import BasketStore
from quanto.workbench.configuration import FrameworkResolver
from quanto.workbench.definitions import DefinitionRegistry
from quanto.workbench.framework_requests import (BasketCreateRequest, BasketRevisionRequest,
                                                  CapabilityCheckRequest)


router = APIRouter()
_capability_request = TypeAdapter(CapabilityCheckRequest)


def _store(request: Request):
    return request.app.state.service.store


def _basket_summary(row):
    return {"basket_id": row["basket_id"], "name": row["name"],
            "current_revision": row["current_revision"], "created_at": row["created_at"]}


def _revision_summary(row):
    return {"revision": row["revision"], "content_hash": row["content_hash"],
            "created_at": row["created_at"]}


@router.get("/baskets")
def baskets(request: Request, cursor: str | None = None, limit: int = 50):
    if not 1 <= limit <= 100:
        raise ValueError("invalid limit")
    offset = int(cursor or "0")
    if offset < 0:
        raise ValueError("invalid cursor")
    with _store(request).transaction() as db:
        rows = db.execute("SELECT * FROM baskets ORDER BY rowid LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
    return {"items": [_basket_summary(row) for row in rows[:limit]],
            "next_cursor": str(offset + limit) if len(rows) > limit else None}


@router.post("/baskets")
def create_basket(request: Request, body: dict):
    value = BasketCreateRequest.model_validate(body)
    created = BasketStore(_store(request)).create(value.name, value.members, value.selection_note,
                                                   value.idempotency_key)
    return created.model_dump(mode="json")


@router.get("/baskets/{identity}")
def basket_detail(request: Request, identity: str):
    with _store(request).transaction() as db:
        row = db.execute("SELECT * FROM baskets WHERE basket_id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        revisions = db.execute("SELECT revision,content_hash,created_at FROM basket_revisions "
                               "WHERE basket_id=? ORDER BY revision", (identity,)).fetchall()
        current = BasketStore(_store(request)).get(BasketRevisionRef(basket_id=identity,
                                                                     revision=row["current_revision"]), db)
    return {**_basket_summary(row), "current": current.model_dump(mode="json"),
            "revisions": [_revision_summary(item) for item in revisions]}


@router.post("/baskets/{identity}/revisions")
def revise_basket(request: Request, identity: str, body: dict):
    value = BasketRevisionRequest.model_validate(body)
    revision = BasketStore(_store(request)).revise(identity, value.expected_revision, value.members,
                                                  value.selection_note)
    return revision.model_dump(mode="json")


@router.get("/baskets/{identity}/revisions/{revision}")
def basket_revision(request: Request, identity: str, revision: int):
    return BasketStore(_store(request)).get(BasketRevisionRef(basket_id=identity, revision=revision)).model_dump(mode="json")


@router.get("/definitions")
def definitions(request: Request):
    return {"items": list(DefinitionRegistry().list())}


@router.post("/capabilities/check")
def check_capabilities(request: Request, body: dict):
    value = _capability_request.validate_python(body)
    return FrameworkResolver(_store(request)).check(value).model_dump(mode="json")
