from __future__ import annotations

import pandas as pd
import pyarrow.parquet as pq

from quanto.data.catalog import compatible
from quanto.data.contracts import BAR_KEY, CoverageEntry, CoverageReport, DataRequest, DataRequirement, FragmentRef, MarketDataError
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
    for instrument in request.instruments:
        excluded = set()
        if not status.empty:
            selected = status[(status.instrument_id == instrument.key) & status.status.isin(["suspended", "not_listed", "delisted"])]
            excluded = set(selected.trading_date)
        listing = None
        if not instruments.empty:
            dates = instruments.loc[instruments.instrument_id == instrument.key, "listing_date"].dropna()
            listing = dates.min() if len(dates) else None
        for day, sessions in calendar.groupby("trading_date", sort=True):
            if day in excluded or (listing and day < listing):
                continue
            if request.frequency == "1d":
                yield instrument.key, sessions.session_start.min(), sessions.session_end.max()
            else:
                for session in sessions.itertuples(index=False):
                    for begin in pd.date_range(session.session_start, session.session_end, freq="min", inclusive="left"):
                        yield instrument.key, begin, begin + pd.Timedelta(minutes=1)


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
                bases = {ref.request.price_basis.model_dump_json() for ref in refs}
                if len(bases) > 1:
                    reason = "SEMANTIC_CONFLICT"
                elif calendar.empty:
                    reason = "CALENDAR_NOT_PREPARED"
                else:
                    try:
                        data = deduplicate(data, BAR_KEY)
                    except MarketDataError:
                        reason = "SEMANTIC_CONFLICT"
                    observed = set(data[list(BAR_KEY)].itertuples(index=False, name=None))
                    expected = list(expected_bars(request, calendar, status, instruments))
                    if observed - set(expected):
                        reason = reason or "BAR_OUTSIDE_SESSION"
                    for key, begin, finish in expected:
                        if (key, begin, finish) not in observed:
                            missing.append(begin.to_pydatetime())
                            by_instrument.setdefault(key, []).append(begin.to_pydatetime())
                    for column in request.columns:
                        if column in data and data[column].isna().any():
                            fields.add(column)
            else:
                proof = {"trading_status": "status", "corporate_actions": "actions"}.get(request.kind, request.kind)
                verified = [ref for ref in refs if proof in ref.source.verified]
                if not covers_interval(verified, request):
                    evidence.add(proof)
                if request.kind == "instruments" and not {item.key for item in request.instruments}.issubset(set(data.instrument_id)):
                    reason = "MISSING_INSTRUMENT"
        entries.append(CoverageEntry(request=request, missing=tuple(sorted(set(missing))), missing_by_instrument={key: tuple(value) for key, value in by_instrument.items()}, missing_fields=tuple(sorted(fields)),
            missing_evidence=tuple(sorted(evidence)), reason=reason))
    return CoverageReport(entries=tuple(entries), limitations=tuple(sorted(limitations)))
