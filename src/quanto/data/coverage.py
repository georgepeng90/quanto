from __future__ import annotations

import pandas as pd
import pyarrow.parquet as pq

from quanto.data.catalog import compatible
from quanto.data.contracts import BAR_KEY, CoverageEntry, CoverageReport, DataRequest, DataRequirement, FragmentRef, MarketDataError, content_key
from quanto.data.storage import MarketStore
from quanto.data.validation import deduplicate


def fragment_frame(store: MarketStore, fragment: FragmentRef) -> pd.DataFrame:
    store.verify_fragment(fragment)
    return pq.read_table(store.object_path(fragment)).to_pandas()


def covers_interval(fragments, request) -> bool:
    cursor = request.start
    for fragment in sorted(fragments, key=lambda item: item.request.start):
        if fragment.request.start > cursor:
            return False
        cursor = max(cursor, fragment.request.end)
        if cursor >= request.end:
            return True
    return False


def expected_bars(request: DataRequest, calendar: pd.DataFrame, status: pd.DataFrame, instruments: pd.DataFrame):
    calendar = calendar[(calendar.session_start >= request.start) & (calendar.session_end <= request.end)]
    exclusions = {}
    if not status.empty:
        excluded = status.loc[status.status.isin(["suspended", "not_listed", "delisted"])]
        exclusions = excluded.groupby("instrument_id").trading_date.agg(set).to_dict()
    listings = instruments.dropna(subset=["listing_date"]).groupby("instrument_id").listing_date.min().to_dict() if not instruments.empty else {}
    schedule = []
    if request.frequency == "1d":
        daily = calendar.groupby("trading_date", sort=True).agg(begin=("session_start", "min"), finish=("session_end", "max"))
        schedule = list(daily.itertuples(index=True, name=None))
    else:
        for session in calendar.itertuples(index=False):
            schedule.extend((session.trading_date, begin, begin + pd.Timedelta(minutes=1))
                for begin in pd.date_range(session.session_start, session.session_end, freq="min", inclusive="left"))
    for instrument in request.instruments:
        excluded = exclusions.get(instrument.key, set())
        listing = listings.get(instrument.key)
        for day, begin, finish in schedule:
            if day in excluded or (listing and day < listing):
                continue
            yield instrument.key, begin, finish, day


def evaluate_coverage(requirement: DataRequirement, fragments: tuple[FragmentRef, ...], store: MarketStore) -> CoverageReport:
    frames = {index: fragment_frame(store, fragment) for index, fragment in enumerate(fragments)}
    def reference(kind, evidence):
        selected = [frames[index] for index, item in enumerate(fragments) if item.request.kind == kind and evidence in item.source.verified]
        return pd.concat(selected, ignore_index=True).drop_duplicates() if selected else pd.DataFrame()
    calendar = reference("calendar", "calendar")
    status = reference("trading_status", "status")
    instruments = reference("instruments", "instruments")
    entries, limitations = [], set()
    for request in requirement.requests:
        selected = [(item, frames[index]) for index, item in enumerate(fragments) if compatible(item.request, request)]
        refs = [item for item, _ in selected]
        missing, by_instrument, fields, evidence, reason = [], {}, set(), set(), None
        for ref in refs:
            limitations.update(ref.source.limitations)
            evidence.update(f"unaccepted:{value}" for value in ref.source.limitations if value not in requirement.quality.accepted_limitations)
            evidence.update(value for value in requirement.quality.required_evidence if value not in ref.source.verified and
                            not (request.kind != "bars" and value in {"units", "price_basis"}))
        if not refs:
            reason = "NO_LOCAL_DATA"
            fields.update(request.columns)
        else:
            data = pd.concat([frame for _, frame in selected], ignore_index=True)
            if request.kind == "bars":
                data = data[(data.bar_start >= request.start) & (data.bar_end <= request.end)]
            fields.update(set(request.columns) - set(data.columns))
            if request.kind == "bars":
                bases_by_instrument = {}
                for ref in refs:
                    for instrument in ref.request.instruments:
                        semantics = content_key([ref.request.price_basis.model_dump(mode="json"), ref.source.units])
                        bases_by_instrument.setdefault(instrument.key, set()).add(semantics)
                if any(len(bases) > 1 for bases in bases_by_instrument.values()):
                    reason = "SEMANTIC_CONFLICT"
                elif any(ref.request.price_basis.mode == "vendor_research" and not ref.request.price_basis.basis_id for ref in refs):
                    reason = "UNRESOLVED_PRICE_BASIS"
                elif calendar.empty:
                    reason = "CALENDAR_NOT_PREPARED"
                else:
                    try:
                        data = deduplicate(data, BAR_KEY)
                    except MarketDataError:
                        reason = "SEMANTIC_CONFLICT"
                    observed_dates = dict(zip(data[list(BAR_KEY)].itertuples(index=False, name=None), data.trading_date))
                    observed = set(observed_dates)
                    expected = {(key, begin, finish): day for key, begin, finish, day in expected_bars(request, calendar, status, instruments)}
                    if observed - set(expected):
                        reason = reason or "BAR_OUTSIDE_SESSION"
                    if any(observed_dates[key] != expected[key] for key in observed.intersection(expected)):
                        reason = reason or "TRADING_DATE_MISMATCH"
                    for key, begin, finish in expected:
                        if (key, begin, finish) not in observed:
                            missing.append(begin.to_pydatetime())
                            by_instrument.setdefault(key, []).append(begin.to_pydatetime())
                    for column in request.columns:
                        if column in data and data[column].isna().any():
                            fields.add(column)
            else:
                proof = {"trading_status": "status", "corporate_actions": "actions"}.get(request.kind, request.kind)
                verified = [(ref, frame) for ref, frame in selected if proof in ref.source.verified]
                for instrument in request.instruments or (None,):
                    scope = [(ref, frame) for ref, frame in verified if instrument is None or not ref.request.instruments or instrument in ref.request.instruments]
                    if not covers_interval([ref for ref, _ in scope], request):
                        evidence.add(proof)
                    for column in request.columns:
                        if not covers_interval([ref for ref, frame in scope if column in frame.columns], request):
                            fields.add(column)
                if request.kind == "instruments" and not {item.key for item in request.instruments}.issubset(set(data.instrument_id)):
                    reason = "MISSING_INSTRUMENT"
        entries.append(CoverageEntry(request=request, missing=tuple(sorted(set(missing))), missing_by_instrument={key: tuple(value) for key, value in by_instrument.items()}, missing_fields=tuple(sorted(fields)),
            missing_evidence=tuple(sorted(evidence)), reason=reason))
    return CoverageReport(entries=tuple(entries), limitations=tuple(sorted(limitations)))
