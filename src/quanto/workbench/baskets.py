"""Immutable ordered basket revisions with atomic compare-and-swap publication."""
from __future__ import annotations

from uuid import uuid4
from contextlib import nullcontext

from quanto.data.contracts import InstrumentId
from quanto.framework import BasketRevisionRef, FrozenBasketRevision, SelectionOrigin, canonical_hash
from quanto.workbench.framework_requests import BasketCreateRequest, BasketRevisionRequest
from quanto.workbench.store import WorkbenchStore, utc_now


class BasketStore:
    def __init__(self, store: WorkbenchStore):
        self.store = store

    def create(self, name: str, members: tuple[InstrumentId, ...], selection_note: str, key: str,
               *, selection_origin: SelectionOrigin | None = None, db=None) -> FrozenBasketRevision:
        request = BasketCreateRequest(name=name, members=members, selection_note=selection_note, idempotency_key=key)
        scope = "basket.create"
        values = {"scope": scope, **request.model_dump(mode="json", exclude={"idempotency_key"})}
        if selection_origin is not None:
            values["selection_origin"] = selection_origin.model_dump(mode="json")
        fingerprint = canonical_hash(values)
        with (nullcontext(db) if db is not None else self.store.transaction()) as db:
            previous = db.execute("SELECT fingerprint,response_json FROM framework_idempotency WHERE scope=? AND key=?", (scope, key)).fetchone()
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("basket idempotency conflict")
                return self.get(BasketRevisionRef.model_validate_json(previous["response_json"]), db)
            revision = FrozenBasketRevision(basket_id=str(uuid4()), revision=1, members=request.members,
                                            created_at=utc_now(), selection_note=request.selection_note,
                                            selection_origin=selection_origin)
            db.execute("INSERT INTO baskets VALUES (?,?,?,?)", (revision.basket_id, request.name, 1, revision.created_at.isoformat()))
            self._insert(db, revision)
            db.execute("INSERT INTO framework_idempotency VALUES (?,?,?,?)", (scope, key, fingerprint, revision.ref.model_dump_json()))
            return revision

    def revise(self, basket_id: str, expected_revision: int, members: tuple[InstrumentId, ...], selection_note: str) -> FrozenBasketRevision:
        request = BasketRevisionRequest(expected_revision=expected_revision, members=members, selection_note=selection_note)
        revision = FrozenBasketRevision(basket_id=basket_id, revision=request.expected_revision + 1, members=request.members,
                                        created_at=utc_now(), selection_note=request.selection_note)
        with self.store.transaction() as db:
            result = db.execute("UPDATE baskets SET current_revision=? WHERE basket_id=? AND current_revision=?",
                                (revision.revision, basket_id, request.expected_revision))
            if result.rowcount != 1:
                raise ValueError("basket revision conflict")
            self._insert(db, revision)
        return revision

    def get(self, ref: BasketRevisionRef, db=None) -> FrozenBasketRevision:
        if db is None:
            with self.store.transaction() as connection:
                return self.get(ref, connection)
        row = db.execute("SELECT * FROM basket_revisions WHERE basket_id=? AND revision=?", (ref.basket_id, ref.revision)).fetchone()
        if row is None:
            raise KeyError(f"{ref.basket_id}@{ref.revision}")
        revision = FrozenBasketRevision.model_validate_json(row["revision_json"])
        if revision.ref != ref or revision.content_hash != row["content_hash"] or revision.created_at.isoformat() != row["created_at"]:
            raise ValueError("BINDING_MISMATCH: stored basket revision differs from immutable content")
        return revision

    @staticmethod
    def _insert(db, revision: FrozenBasketRevision) -> None:
        db.execute("INSERT INTO basket_revisions VALUES (?,?,?,?,?)",
                   (revision.basket_id, revision.revision, revision.model_dump_json(), revision.content_hash, revision.created_at.isoformat()))
