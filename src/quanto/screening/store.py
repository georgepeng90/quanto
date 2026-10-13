"""Immutable research archives, with transactional publication and idempotency."""
from datetime import datetime
import re
from uuid import uuid4

from pydantic import field_validator, model_validator

from quanto.data.contracts import utc
from quanto.framework import FrozenContract, Hash, SelectionOrigin, canonical_hash
from quanto.objects import digest, json_bytes, verified_files
from quanto.screening.contracts import Key, ScreeningRequest, ScreeningResult
from quanto.screening.engine import screen
from quanto.workbench.revisions import publish_files
from quanto.workbench.store import WorkbenchStore, utc_now


class ScreeningRecord(FrozenContract):
    id: Key
    created_at: datetime
    request: ScreeningRequest
    result: ScreeningResult
    result_hash: Hash

    _created = field_validator("created_at")(utc)

    @model_validator(mode="after")
    def verify(self):
        if canonical_hash(self.result.model_dump(mode="json")) != self.result_hash or screen(self.request) != self.result:
            raise ValueError("screening result hash or replay mismatch")
        return self


class ScreeningStore:
    def __init__(self, store: WorkbenchStore):
        self.store = store

    def submit(self, request: ScreeningRequest) -> ScreeningRecord:
        result = screen(request)
        with self.store.transaction() as db:
            previous = db.execute("SELECT * FROM screening_idempotency WHERE key=?", (request.idempotency_key,)).fetchone()
            if previous:
                if previous["fingerprint"] != result.input_hash:
                    raise ValueError("screening idempotency conflict")
                return self.get(previous["record_id"], db)
            record = ScreeningRecord(id=str(uuid4()), created_at=utc_now(), request=request, result=result,
                                     result_hash=canonical_hash(result.model_dump(mode="json")))
            content = json_bytes(record.model_dump(mode="json"))
            key = digest(content)
            publish_files(self.store.root / "screenings", key, {"record.json": content})
            db.execute("INSERT INTO screening_records VALUES (?,?,?,?,?,?)",
                       (record.id, request.name, request.as_of.isoformat(), record.result_hash,
                        f"screenings/{key}", record.created_at.isoformat()))
            db.execute("INSERT INTO screening_idempotency VALUES (?,?,?)",
                       (request.idempotency_key, result.input_hash, record.id))
            return record

    def get(self, identity: str, db=None) -> ScreeningRecord:
        if db is None:
            with self.store.transaction() as connection:
                return self.get(identity, connection)
        row = db.execute("SELECT * FROM screening_records WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        match = re.fullmatch(r"screenings/([0-9a-f]{64})", row["artifact_path"])
        if not match:
            raise ValueError("invalid screening artifact path")
        files = verified_files(self.store.root / row["artifact_path"], {"record.json": match[1]})
        record = ScreeningRecord.model_validate_json(files["record.json"].read_bytes())
        if (record.id, record.request.name, record.request.as_of.isoformat(), record.result_hash, record.created_at.isoformat()) != (
                row["id"], row["name"], row["as_of"], row["result_hash"], row["created_at"]):
            raise ValueError("screening index differs from immutable record")
        return record

    def list(self, cursor: str | None = None, limit: int = 50) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("invalid limit")
        offset = int(cursor or "0")
        if offset < 0:
            raise ValueError("invalid cursor")
        with self.store.transaction() as db:
            rows = db.execute("SELECT id FROM screening_records ORDER BY rowid LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
            records = [self.get(row["id"], db) for row in rows[:limit]]
        return {"items": [{"id": item.id, "name": item.request.name, "as_of": item.request.as_of.isoformat(),
                           "selected_count": len(item.result.members), "candidate_count": len(item.result.candidates),
                           "shortfall": item.result.shortfall} for item in records],
                "next_cursor": str(offset + limit) if len(rows) > limit else None}

    def export(self, identity: str) -> bytes:
        return json_bytes(self.get(identity).model_dump(mode="json"))

    def validate_basket(self, basket, db=None) -> ScreeningRecord | None:
        if basket.selection_origin is None:
            return None
        record = self.get(basket.selection_origin.screening_id, db)
        validate_selection(basket, record.model_dump(mode="json"))
        return record

    def create_basket(self, identity: str, name: str, key: str):
        from quanto.workbench.baskets import BasketStore
        with self.store.transaction() as db:
            record = self.get(identity, db)
            if not record.result.members:
                raise ValueError("screening has no selected members")
            origin = SelectionOrigin(screening_id=record.id, result_hash=record.result_hash, as_of=record.request.as_of)
            return BasketStore(self.store).create(name, record.result.members, record.request.purpose, key,
                                                   selection_origin=origin, db=db)


def validate_selection(basket, frozen_record):
    """Prove origin and ordered members using only the archived record."""
    origin = basket.selection_origin
    if origin is None:
        if frozen_record is not None:
            raise ValueError("unexpected screening record")
        return None
    if frozen_record is None:
        raise ValueError("screening archive is required")
    record = ScreeningRecord.model_validate(frozen_record)
    if (origin.screening_id, origin.result_hash, origin.as_of) != (record.id, record.result_hash, record.request.as_of):
        raise ValueError("screening origin mismatch")
    if basket.members != record.result.members:
        raise ValueError("screening members differ from basket")
    return record
