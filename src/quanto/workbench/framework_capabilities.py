"""Read-only checks against frozen data, temporal, and execution declarations."""
from __future__ import annotations

from datetime import date
import math

import pandas as pd

from quanto.backtest.dataset_schemas import DatasetBundle
from quanto.backtest.trading import TradingDefinition
from quanto.config import BacktestSpec
from quanto.data.point_in_time import CORE_TABLES, TableSemantics, _visible, session_cutoff
from quanto.framework import AssetBinding, AssetMapping, CapabilityCheck, RequirementItem, ResearchInterval


def _check(name: str, ok: bool, path: str, code: str, message: str) -> CapabilityCheck:
    return CapabilityCheck(name=name, status="passed" if ok else "blocked", field_path=path,
                           code=None if ok else code, message=message)


def _error_code(exc: ValueError, default: str) -> str:
    prefix = str(exc).split(":", 1)[0]
    return prefix if prefix in {"IDENTITY_UNRESOLVED", "BINDING_MISMATCH", "MIXED_BASKET_UNSUPPORTED",
                                "DEFINITION_UNAVAILABLE", "DATA_REQUIREMENT_UNSATISFIED",
                                "TEMPORAL_EVIDENCE_MISSING", "EXECUTION_CAPABILITY_MISSING",
                                "MODEL_IDENTITY_MISMATCH"} else default


def _table(bundle: DatasetBundle, name: str) -> pd.DataFrame | None:
    if name in CORE_TABLES:
        return getattr(bundle, name)
    return bundle.auxiliary.get(name)


def _days(frame: pd.DataFrame, column: str) -> set[date]:
    try:
        values = pd.to_datetime(frame[column], errors="coerce")
    except (ValueError, TypeError):
        return set()
    return set(values.dropna().dt.date)


def _selected(frame: pd.DataFrame, symbol_column: str | None, symbols: tuple[str, ...]) -> pd.DataFrame:
    if symbol_column and symbol_column in frame:
        return frame.loc[frame[symbol_column].isin(symbols)]
    return frame


def _visible_at(frame: pd.DataFrame, semantics: TableSemantics, day: date,
                sessions: tuple[date, ...], dataset_id: str) -> pd.DataFrame:
    return _visible(frame, semantics, session_cutoff(day), sessions, dataset_id)


def _stable_daily_at(frame: pd.DataFrame, semantics: TableSemantics, day: date,
                     sessions: tuple[date, ...]) -> pd.DataFrame:
    """Check canonical daily facts without computing unused row provenance hashes."""
    rules = semantics.rules
    required = {*rules.business_keys, *semantics.binding.schema_definition}
    required.update(value for value in (rules.symbol_column, rules.date_column,
        rules.available_column, rules.revision_id_column, rules.revision_order_column,
        rules.effective_from_column, rules.effective_to_column) if value)
    if not required.issubset(frame.columns):
        raise ValueError(f"TEMPORAL_EVIDENCE_MISSING: missing declared fields in {semantics.binding.table}")
    frame = frame.loc[:, sorted(required)]
    dates = frame[rules.date_column].dt.date
    if dates.isna().any() or not dates.isin(sessions).all():
        raise ValueError("TEMPORAL_EVIDENCE_MISSING: observation date absent from trading calendar")
    visible = frame.loc[dates <= day]
    if not visible.empty:
        keys = list(rules.business_keys)
        if visible[keys].isna().any().any():
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: missing business identity")
        if visible.duplicated(keys).any():
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: duplicate facts without revision ordering")
    return visible


def _invalid_values(series: pd.Series, dtype: str) -> bool:
    if series.isna().any():
        return True
    if dtype in {"float", "int"}:
        values = pd.to_numeric(series, errors="coerce")
        return (values.isna().any() or not all(math.isfinite(float(value)) for value in values)
                or (dtype == "int" and any(float(value) % 1 for value in values)))
    if dtype == "bool":
        return any(type(value) is not bool for value in series)
    if dtype == "string":
        return any(not isinstance(value, str) or not value for value in series)
    if dtype in {"date", "datetime"}:
        return pd.to_datetime(series, errors="coerce").isna().any()
    return True


def check_requirements(
    bundle: DatasetBundle,
    binding: AssetBinding,
    ordered_pairs: tuple[AssetMapping, ...],
    requirements: tuple[RequirementItem, ...],
    trading_definition: TradingDefinition,
    backtest_spec: BacktestSpec,
    *,
    table_semantics: tuple[TableSemantics, ...] | None = None,
) -> tuple[CapabilityCheck, ...]:
    """Check only the frozen selected basket; never fetch data or mutate the bundle."""
    checks: list[CapabilityCheck] = []
    symbols = tuple(pair.local_symbol for pair in ordered_pairs)
    by_identity = {pair.instrument_id.key: pair for pair in binding.assets}
    valid_pairs = bool(ordered_pairs) and len(set(symbols)) == len(symbols) and all(
        by_identity.get(pair.instrument_id.key) == pair for pair in ordered_pairs)
    checks.append(_check("selected_binding", valid_pairs, "ordered_pairs", "DATA_REQUIREMENT_UNSATISFIED",
                         "Every selected member needs its exact frozen dataset mapping."))
    if not valid_pairs:
        return tuple(checks)
    checks.append(_check("run_symbols", tuple(backtest_spec.symbols) == symbols, "backtest_spec.symbols",
                         "BINDING_MISMATCH", "Run symbols must equal selected local symbols in order."))
    types = {pair.instrument_id.instrument_type for pair in ordered_pairs}
    if types == {"stock"}:
        from quanto.backtest.stock_data import validate_stock_data
        try:
            validate_stock_data(bundle, binding, symbols, ResearchInterval(
                start=backtest_spec.start, end=backtest_spec.end, warmup_start=backtest_spec.start))
        except ValueError as exc:
            checks.append(_check("stock_execution_evidence", False, "dataset.auxiliary", "DATA_REQUIREMENT_UNSATISFIED", str(exc)))
    checks.append(_check("basket_type", len(types) == 1, "ordered_pairs", "MIXED_BASKET_UNSUPPORTED",
                         "The execution definition needs one instrument type per run."))
    checks.append(_check("trading_assets", types.issubset(trading_definition.supported_instrument_types),
                         "trading_definition", "EXECUTION_CAPABILITY_MISSING", "Trading definition must support selected instruments."))
    checks.append(_check("trading_price_mode", bundle.price_mode in trading_definition.supported_price_modes,
                         "trading_definition", "EXECUTION_CAPABILITY_MISSING", "Trading definition must support the dataset price mode."))
    checks.append(_check("raw_events", bundle.price_mode != "raw_cash" or bundle.manifest.event_coverage == "complete",
                         "dataset.event_coverage", "EXECUTION_CAPABILITY_MISSING", "Raw cash prices require complete event coverage."))

    if "date" not in bundle.calendar or bundle.calendar.empty:
        checks.append(_check("calendar", False, "dataset.calendar", "DATA_REQUIREMENT_UNSATISFIED",
                             "Trading sessions are unavailable."))
        return tuple(checks)
    sessions = tuple(sorted(_days(bundle.calendar, "date")))
    run_days = tuple(day for day in sessions if backtest_spec.start <= day <= backtest_spec.end)
    covered = bundle.manifest.data_start <= backtest_spec.start <= backtest_spec.end <= bundle.manifest.data_end
    checks.append(_check("run_coverage", covered and bool(run_days), "backtest_spec", "DATA_REQUIREMENT_UNSATISFIED",
                         "Run interval must have sessions inside dataset coverage."))

    if not {"symbol", "listing_date"}.issubset(bundle.instruments.columns):
        checks.append(_check("instrument_reference", False, "dataset.instruments", "EXECUTION_CAPABILITY_MISSING",
                             "Instrument listing dates are required."))
        return tuple(checks)
    instruments = bundle.instruments.loc[bundle.instruments.symbol.isin(symbols)]
    listings: dict[str, date] = {}
    for symbol in symbols:
        rows = instruments.loc[instruments.symbol == symbol]
        if len(rows) == 1:
            stamp = pd.to_datetime(rows.iloc[0].listing_date, errors="coerce")
            if not pd.isna(stamp):
                listings[symbol] = stamp.date()
    checks.append(_check("instrument_reference", len(listings) == len(symbols), "dataset.instruments",
                         "EXECUTION_CAPABILITY_MISSING", "Each selected member needs one known listing date."))

    status_rows: dict[tuple[str, date], list[dict]] = {}
    if {"symbol", "date"}.issubset(bundle.status.columns):
        selected_status = bundle.status.loc[bundle.status.symbol.isin(symbols)]
        for row in selected_status.to_dict("records"):
            stamp = pd.to_datetime(row["date"], errors="coerce")
            if not pd.isna(stamp):
                status_rows.setdefault((row["symbol"], stamp.date()), []).append(row)
    price_days: dict[str, set[date]] = {}
    if {"symbol", "date"}.issubset(bundle.prices.columns):
        price_days = {symbol: _days(group, "date") for symbol, group in
                      bundle.prices.loc[bundle.prices.symbol.isin(symbols)].groupby("symbol")}
    daily_state: dict[tuple[str, date], str] = {}
    for day in run_days:
        for symbol in symbols:
            if symbol not in listings or day < listings[symbol]:
                continue
            status = status_rows.get((symbol, day), [])
            path = f"dataset.status[{symbol},{day.isoformat()}]"
            if len(status) != 1:
                checks.append(_check("trading_status", False, path, "EXECUTION_CAPABILITY_MISSING",
                                     "Listed member must have exactly one daily trading state."))
                continue
            try:
                trading_definition.eligibility(status[0])
            except (ValueError, KeyError) as exc:
                checks.append(_check("trading_status", False, path, "EXECUTION_CAPABILITY_MISSING", str(exc)))
                continue
            daily_state[symbol, day] = status[0]["status"]
            if status[0]["status"] == "trading":
                if day not in price_days.get(symbol, set()):
                    checks.append(_check("price_observation", False, f"dataset.prices[{symbol},{day.isoformat()}]",
                                         "DATA_REQUIREMENT_UNSATISFIED", "Trading session has no observed price."))

    if not bundle.events.empty:
        if not {"symbol", "record_date", "ex_date", "pay_date"}.issubset(bundle.events.columns):
            checks.append(_check("execution_events", False, "dataset.events", "EXECUTION_CAPABILITY_MISSING",
                                 "Execution events lack required date or symbol fields."))
        else:
            events = bundle.events.loc[bundle.events.symbol.isin(symbols)].copy()
            if not events.empty:
                dates = events[["record_date", "ex_date", "pay_date"]].apply(lambda col: pd.to_datetime(col, errors="coerce"))
                relevant = dates.apply(lambda col: col.between(pd.Timestamp(backtest_spec.start), pd.Timestamp(backtest_spec.end))).any(axis=1)
                events = events.loc[relevant | dates.isna().all(axis=1)]
            try:
                trading_definition.events(events, price_mode=bundle.price_mode)
            except (ValueError, KeyError, TypeError) as exc:
                checks.append(_check("execution_events", False, "dataset.events", "EXECUTION_CAPABILITY_MISSING", str(exc)))

    declarations = {item.table: item for item in binding.table_semantics}
    readers = {item.binding.table: item for item in table_semantics or ()}
    for index, requirement in enumerate(requirements):
        path = f"requirements[{index}]"
        if bundle.price_mode not in requirement.price_modes:
            checks.append(_check("price_mode", False, f"{path}.price_modes", "DATA_REQUIREMENT_UNSATISFIED",
                                 "Requirement does not accept the dataset price mode."))
        declaration = declarations.get(requirement.table)
        frame = _table(bundle, requirement.table)
        may_mask = not requirement.required and requirement.missing_policy == "mask"
        interval_covered = (bundle.manifest.calendar_start <= requirement.interval.warmup_start and
                            bundle.manifest.data_start <= requirement.interval.start <= requirement.interval.end <= bundle.manifest.data_end)
        checks.append(_check("requirement_interval", interval_covered, f"{path}.interval",
                             "DATA_REQUIREMENT_UNSATISFIED", "Requirement interval exceeds the dataset calendar or data coverage."))
        if frame is None or declaration is None:
            checks.append(_check("table_presence", may_mask and frame is None, f"{path}.table",
                                 "DATA_REQUIREMENT_UNSATISFIED", "Required table or frozen declaration is absent."))
            continue
        try:
            if table_semantics is not None:
                reader = readers.get(requirement.table)
                if reader is None:
                    raise ValueError("DEFINITION_UNAVAILABLE: no supplied reader for frozen table")
            else:
                reader = TableSemantics.from_binding(declaration)
            if reader.binding != declaration:
                raise ValueError("BINDING_MISMATCH: reader differs from frozen declaration")
        except ValueError as exc:
            checks.append(_check("table_reader", False, f"{path}.table", _error_code(exc, "DEFINITION_UNAVAILABLE"), str(exc)))
            continue
        checks.append(_check("semantic_version", requirement.semantic_version == declaration.version,
                             f"{path}.semantic_version", "DATA_REQUIREMENT_UNSATISFIED", "Frozen table semantic version must match."))
        checks.append(_check("availability_rule", requirement.availability_rule == reader.rules.availability_rule,
                             f"{path}.availability_rule", "TEMPORAL_EVIDENCE_MISSING", "Frozen availability rule must match."))
        price_mode_matches = (reader.rules.price_mode == bundle.price_mode or
                              (requirement.table not in CORE_TABLES and reader.rules.price_mode is None))
        checks.append(_check("table_price_mode", price_mode_matches,
                             f"{path}.price_modes", "BINDING_MISMATCH", "Frozen price basis must match dataset."))
        if requirement.warmup_sessions and requirement.table != "prices":
            checks.append(_check("warmup_definition", False, f"{path}.warmup_sessions",
                                 "DEFINITION_UNAVAILABLE", "Observation-count warmup is defined only for prices."))
        requirement_symbols = symbols
        if requirement.scope == "reference":
            references = [by_identity.get(item.key) for item in requirement.reference_ids]
            if any(item is None for item in references):
                benchmark_id = binding.benchmark.instrument_id
                if (requirement.table != "benchmark" or benchmark_id is None or
                        any(item.key != benchmark_id.key for item in requirement.reference_ids)):
                    checks.append(_check("reference_binding", False, f"{path}.reference_ids",
                                         "DATA_REQUIREMENT_UNSATISFIED", "Reference identity has no frozen mapping."))
                    continue
            requirement_symbols = tuple(item.local_symbol for item in references if item is not None)
        for field_index, field in enumerate(requirement.fields):
            field_path = f"{path}.fields[{field_index}]"
            declared = declaration.schema_definition.get(field.name)
            if declared is None or field.name not in frame:
                checks.append(_check("field_presence", may_mask and field.name not in frame and declared is None,
                                     field_path, "DATA_REQUIREMENT_UNSATISFIED", f"Field {field.name} is absent."))
                continue
            matches = (declared.get("dtype") == field.dtype and declared.get("unit") == field.unit and
                       (field.currency is None or declared.get("currency") == field.currency) and
                       (field.report_basis is None or declared.get("report_basis") == field.report_basis))
            checks.append(_check("field_contract", matches, field_path, "DATA_REQUIREMENT_UNSATISFIED",
                                 f"Field {field.name} type, unit, currency, or report basis differs."))
        selected = _selected(frame, reader.rules.symbol_column, requirement_symbols)
        temporal_columns = set(reader.rules.business_keys)
        temporal_columns.update(name for name in (reader.rules.symbol_column, reader.rules.date_column,
            reader.rules.available_column, reader.rules.revision_id_column, reader.rules.revision_order_column,
            reader.rules.effective_from_column, reader.rules.effective_to_column) if name)
        if not temporal_columns.issubset(selected.columns):
            checks.append(_check("temporal_columns", False, f"{path}.table", "TEMPORAL_EVIDENCE_MISSING",
                                 "Frozen temporal key or timestamp columns are absent."))
            continue
        rule = reader.rules.availability_rule
        if rule == "execution_event":
            # Nullable cells depend on event type; the execution hook validates relevant rows.
            continue
        if rule == "declared_calendar":
            dates = _days(selected, reader.rules.date_column or "date")
            needed = {day for day in sessions if requirement.interval.warmup_start <= day <= requirement.interval.end}
            checks.append(_check("calendar_coverage", bool(needed) and needed.issubset(dates), f"{path}.interval",
                                 "DATA_REQUIREMENT_UNSATISFIED", "Declared calendar must cover the requested interval."))
            values_frame = selected.loc[pd.to_datetime(selected[reader.rules.date_column], errors="coerce").dt.date.isin(needed)]
            for field_index, field in enumerate(requirement.fields):
                if field.name in values_frame and not values_frame.empty:
                    values = values_frame[field.name]
                    valid = (not values.isna().any() or may_mask) and not _invalid_values(values.dropna(), field.dtype)
                    checks.append(_check("field_values", valid, f"{path}.fields[{field_index}]",
                                         "DATA_REQUIREMENT_UNSATISFIED", f"Field {field.name} has invalid or unavailable values."))
            continue
        if rule == "declared_static_reference":
            present = not selected.empty and (not reader.rules.symbol_column or
                      set(requirement_symbols).issubset(set(selected[reader.rules.symbol_column])))
            checks.append(_check("static_reference", present or may_mask, f"{path}.table",
                                 "DATA_REQUIREMENT_UNSATISFIED", "Selected static reference facts are absent."))
            for field_index, field in enumerate(requirement.fields):
                if field.name in selected and not selected.empty:
                    values = selected[field.name]
                    valid = (not values.isna().any() or may_mask) and not _invalid_values(values.dropna(), field.dtype)
                    checks.append(_check("field_values", valid, f"{path}.fields[{field_index}]",
                                         "DATA_REQUIREMENT_UNSATISFIED", f"Field {field.name} has invalid or unavailable values."))
            continue
        if selected.empty:
            checks.append(_check("observations", may_mask, f"{path}.table", "DATA_REQUIREMENT_UNSATISFIED",
                                 "No selected observations are present."))
            continue
        days = tuple(day for day in sessions if requirement.interval.start <= day <= requirement.interval.end)
        if not days:
            checks.append(_check("observation_coverage", False, f"{path}.interval",
                                 "DATA_REQUIREMENT_UNSATISFIED", "Requirement interval has no trading session."))
            continue
        if requirement.warmup_sessions and requirement.table == "prices":
            warmup_days = tuple(day for day in sessions if requirement.interval.warmup_start <= day < requirement.interval.start)
            try:
                warmup_view = (_visible_at(selected, reader, warmup_days[-1], sessions, binding.dataset_version_id)
                               if warmup_days else selected.iloc[:0])
            except (ValueError, KeyError, TypeError) as exc:
                checks.append(_check("temporal_visibility", False, f"{path}.table", _error_code(exc, "TEMPORAL_EVIDENCE_MISSING"), str(exc)))
                continue
            warmup_view = warmup_view.loc[pd.to_datetime(warmup_view[reader.rules.date_column], errors="coerce").dt.date.isin(warmup_days)]
            for symbol in requirement_symbols:
                member = warmup_view.loc[warmup_view[reader.rules.symbol_column] == symbol]
                count = len(_days(member, reader.rules.date_column or "date"))
                checks.append(_check("price_warmup", count >= requirement.warmup_sessions,
                                     f"{path}.warmup_sessions", "DATA_REQUIREMENT_UNSATISFIED",
                                     f"{symbol} has {count} observed warmup prices; {requirement.warmup_sessions} required."))
            for field_index, field in enumerate(requirement.fields):
                if field.name in warmup_view and not warmup_view.empty:
                    values = warmup_view[field.name]
                    valid = (not values.isna().any() or may_mask) and not _invalid_values(values.dropna(), field.dtype)
                    checks.append(_check("warmup_field_values", valid, f"{path}.fields[{field_index}]",
                                         "DATA_REQUIREMENT_UNSATISFIED", f"Warmup field {field.name} has invalid or unavailable values."))
        stable_daily = rule == "session_close" and reader.rules.revision_policy == "none" and not reader.rules.effective_from_column
        daily_view = None
        daily_groups = {}
        empty_daily = None
        if stable_daily and days:
            try:
                date_values = selected[reader.rules.date_column]
                derived_fields = {'source_available_at', 'source_ref'}
                uses_derived = bool(derived_fields & (temporal_columns | set(declaration.schema_definition) |
                    {field.name for field in requirement.fields}))
                canonical_dates = (isinstance(date_values, pd.Series) and
                    selected.columns.is_unique and pd.api.types.is_datetime64_any_dtype(date_values.dtype) and
                    date_values.dt.tz is None)
                daily_view = (_stable_daily_at(selected, reader, days[-1], sessions)
                    if canonical_dates and not uses_derived else
                    _visible_at(selected, reader, days[-1], sessions, binding.dataset_version_id))
            except (ValueError, KeyError, TypeError) as exc:
                checks.append(_check("temporal_visibility", False, f"{path}.table", _error_code(exc, "TEMPORAL_EVIDENCE_MISSING"), str(exc)))
                continue
            daily_dates = pd.to_datetime(daily_view[reader.rules.date_column], errors="coerce").dt.date
            daily_groups = {day: group for day, group in daily_view.groupby(daily_dates.to_numpy(), sort=False)}
            empty_daily = daily_view.iloc[:0]
        missing_reported = False
        invalid_reported: set[int] = set()
        for day in days:
            try:
                visible = (daily_groups.get(day, empty_daily) if daily_view is not None else
                           _visible_at(selected, reader, day, sessions, binding.dataset_version_id))
            except (ValueError, KeyError, TypeError) as exc:
                checks.append(_check("temporal_visibility", False, f"{path}.table", _error_code(exc, "TEMPORAL_EVIDENCE_MISSING"), str(exc)))
                break
            if daily_view is None and reader.rules.date_column and reader.rules.availability_rule == "session_close":
                visible = visible.loc[pd.to_datetime(visible[reader.rules.date_column], errors="coerce").dt.date == day]
            for field_index, field in enumerate(requirement.fields):
                if field.name in visible and not visible.empty and field_index not in invalid_reported:
                    values = visible[field.name]
                    valid = (not values.isna().any() or may_mask) and not _invalid_values(values.dropna(), field.dtype)
                    if not valid:
                        checks.append(_check("field_values", False, f"{path}.fields[{field_index}]",
                                             "DATA_REQUIREMENT_UNSATISFIED", f"Field {field.name} has invalid or unavailable values at {day.isoformat()} close."))
                        invalid_reported.add(field_index)
            present = not visible.empty
            if reader.rules.symbol_column:
                expected = requirement_symbols
                if requirement.table == "prices":
                    expected = tuple(symbol for symbol in requirement_symbols
                                     if day >= listings.get(symbol, date.max) and daily_state.get((symbol, day)) == "trading")
                elif requirement.table == "status":
                    expected = tuple(symbol for symbol in requirement_symbols
                                     if day >= listings.get(symbol, date.max))
                visible_symbols = set(visible[reader.rules.symbol_column])
                present = all(symbol in visible_symbols for symbol in expected)
            if not present:
                if not missing_reported:
                    checks.append(_check("observation_coverage", may_mask, f"{path}.table",
                                         "DATA_REQUIREMENT_UNSATISFIED", f"Selected facts unavailable at {day.isoformat()} close."))
                    missing_reported = True
    return tuple(checks)
