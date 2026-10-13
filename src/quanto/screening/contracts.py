"""Bounded immutable Agent submissions; source text is data, never executable input."""
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from quanto.data.contracts import InstrumentId, utc
from quanto.framework import FrozenContract, Hash

Text = Annotated[StrictStr, Field(min_length=1, max_length=10000)]
Key = Annotated[StrictStr, Field(min_length=1, max_length=200)]
Count = Annotated[StrictInt, Field(ge=1, le=10000)]
Status = Literal["trading", "st", "suspended", "delisted"]
Fact = Literal["name", "board", "industry", "listing_date", "status", "business_summary"]


class Citation(FrozenContract):
    evidence_id: Key
    quote: Text


class EvidenceSubmission(FrozenContract):
    id: Key
    source_name: Text
    source_kind: Literal["exchange", "company", "filing", "news"]
    source_locator: Text
    text: Annotated[StrictStr, Field(min_length=1, max_length=1000000)]
    published_at: datetime | date | None
    retrieved_at: datetime
    instruments: Annotated[tuple[InstrumentId, ...], Field(min_length=1, max_length=10000)]
    archive_level: Literal["original", "excerpt"]
    revises: Key | None = None

    _retrieved = field_validator("retrieved_at")(utc)

    @field_validator("published_at", mode="before")
    @classmethod
    def date_only(cls, value):
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else value

    @field_validator("published_at")
    @classmethod
    def publication(cls, value):
        return utc(value) if isinstance(value, datetime) else value


class ScreeningRules(FrozenContract):
    allowed_boards: Annotated[tuple[Literal["main_board"], ...], Field(min_length=1, max_length=1)]
    min_listing_days: Annotated[StrictInt, Field(ge=0, le=100000)]
    liquidity_window: Count
    min_average_amount: Annotated[float, Field(ge=0, le=1e15, strict=True)]
    min_history_days: Count
    excluded_statuses: tuple[Status, ...]
    industry_max: Count
    target_count: Count


class DailyFact(FrozenContract):
    date: date
    close: Annotated[float, Field(gt=0, strict=True)]
    volume: Annotated[float, Field(ge=0, strict=True)]
    amount: Annotated[float, Field(ge=0, strict=True)]
    amount_unit: Key
    citations: tuple[Citation, ...]


class CandidateResearch(FrozenContract):
    instrument_id: InstrumentId
    name: Text
    board: Key | None
    industry: Key | None
    listing_date: date | None
    status: Status | None
    status_date: date | None
    citations: dict[Fact, tuple[Citation, ...]]
    daily: Annotated[tuple[DailyFact, ...], Field(max_length=10000)]
    recommendation: Literal["include", "exclude", "not_researched"]
    rank: Annotated[StrictInt, Field(ge=0)]
    business_summary: str
    reasons: tuple[Text, ...]
    risks: tuple[Text, ...]
    counter_evidence: tuple[Text, ...]
    unknowns: tuple[Text, ...]

    @model_validator(mode="after")
    def unique_days(self):
        if len({item.date for item in self.daily}) != len(self.daily):
            raise ValueError("duplicate daily fact")
        return self


class ScreeningRequest(FrozenContract):
    name: Key
    purpose: Text
    as_of: datetime
    universe_description: Text
    universe_kind: Literal["provided_list", "subjective_discovery", "claimed_full_market"]
    universe_citation: Citation | None = None
    trading_days: Annotated[tuple[date, ...], Field(max_length=20000)]
    rules: ScreeningRules
    evidence: Annotated[tuple[EvidenceSubmission, ...], Field(max_length=1000)]
    candidates: Annotated[tuple[CandidateResearch, ...], Field(min_length=1, max_length=10000)]
    agent_model: Key
    prompt_version: Key
    idempotency_key: Key

    _as_of = field_validator("as_of")(utc)

    @model_validator(mode="after")
    def references(self):
        sources = {item.id: item for item in self.evidence}
        if len(sources) != len(self.evidence):
            raise ValueError("duplicate evidence id")
        if len({c.instrument_id.key for c in self.candidates}) != len(self.candidates):
            raise ValueError("duplicate instrument identity")
        if tuple(sorted(set(self.trading_days))) != self.trading_days:
            raise ValueError("trading_days must be unique and ordered")
        if self.universe_kind == "claimed_full_market" and self.universe_citation is None:
            raise ValueError("full-market claim requires archived universe citation")
        def verify(citation, identity=None):
            source = sources.get(citation.evidence_id)
            if source is None or citation.quote not in source.text:
                raise ValueError("citation must quote archived evidence")
            if identity is not None and identity not in source.instruments:
                raise ValueError("citation instrument mismatch")
        if self.universe_citation:
            verify(self.universe_citation)
        for candidate in self.candidates:
            citations = [c for items in candidate.citations.values() for c in items]
            citations.extend(c for bar in candidate.daily for c in bar.citations)
            for citation in citations:
                verify(citation, candidate.instrument_id)
        for source in self.evidence:
            visited = {source.id}
            previous = source.revises
            while previous:
                if previous not in sources or previous in visited:
                    raise ValueError("invalid evidence revision chain")
                visited.add(previous)
                previous = sources[previous].revises
        return self


class EvidenceDigest(FrozenContract):
    id: Key
    content_hash: Hash
    archive_level: Literal["original", "excerpt"]
    available_at: datetime | None


class CandidateResult(FrozenContract):
    instrument_id: InstrumentId
    decision: Literal["selected", "excluded", "not_researched"]
    reasons: tuple[str, ...]
    valid_history_days: int
    average_amount: float | None


class ScreeningResult(FrozenContract):
    implementation_version: Literal["stock-screening@1"] = "stock-screening@1"
    input_hash: Hash
    members: tuple[InstrumentId, ...]
    candidates: tuple[CandidateResult, ...]
    evidence: tuple[EvidenceDigest, ...]
    shortfall: int
    historical_selection_validation: Literal[False] = False
    evidence_assurance: Literal["agent_supplied_citations_not_independently_verified"] = "agent_supplied_citations_not_independently_verified"
    source_priority: tuple[str, ...] = ("exchange", "company", "filing", "news")
