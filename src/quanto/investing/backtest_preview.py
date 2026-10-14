"""Read-only, evidence-checked date proposals for a saved policy backtest."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from quanto.backtest.dataset_loaders import load_dataset
from quanto.workbench.bindings import BindingStore
from quanto.workbench.configuration import FrameworkResolver, _blocked, _model_code, _report
from quanto.workbench.dataset_objects import verify_dataset_version
from quanto.workbench.framework_requests import PolicyBacktestRequest


def _days(frame):
    return set(pd.to_datetime(frame.date).dt.date)


def _candidate_runs(bundle, asset_binding, symbols, requirements, start, end):
    """Split on missing execution facts, retaining the original calendar adjacency."""
    calendar = sorted(_days(bundle.calendar))
    days = [day for day in calendar if start <= day <= end
            and bundle.manifest.data_start <= day <= bundle.manifest.data_end]
    usable = set(days)
    status = bundle.status.loc[bundle.status.symbol.isin(symbols)].copy()
    status['day'] = pd.to_datetime(status.date).dt.date
    for symbol in symbols:
        member = status.loc[status.symbol == symbol]
        unique = member.groupby('day').size()
        listing = pd.Timestamp(bundle.instruments.loc[bundle.instruments.symbol == symbol,
                                                      'listing_date'].iloc[0]).date()
        usable.intersection_update(set(unique[unique == 1].index) | {day for day in days if day < listing})
        trading = set(member.loc[member.status == 'trading', 'day'])
        prices = _days(bundle.prices.loc[bundle.prices.symbol == symbol])
        usable.difference_update(trading - prices)

    declarations = {item.table: item for item in asset_binding.table_semantics}
    for requirement in requirements:
        if not requirement.required or requirement.table in {'prices', 'status'}:
            continue
        declaration = declarations.get(requirement.table)
        if declaration is None:
            continue  # The authoritative resolver reports absent contracts.
        rules = declaration.temporal_rules
        if rules.get('availability_rule') != 'session_close':
            continue
        frame = getattr(bundle, requirement.table, None)
        if frame is None:
            frame = bundle.auxiliary.get(requirement.table)
        date_column = rules.get('date_column', 'date')
        symbol_column = rules.get('symbol_column')
        if frame is None or date_column not in frame:
            continue
        if symbol_column:
            selected = symbols
            if requirement.scope == 'reference':
                selected = tuple(asset.local_symbol for asset in asset_binding.assets
                                 if asset.instrument_id in requirement.reference_ids)
            for symbol in selected:
                usable.intersection_update(set(pd.to_datetime(
                    frame.loc[frame[symbol_column] == symbol, date_column]).dt.date))
        else:
            usable.intersection_update(set(pd.to_datetime(frame[date_column]).dt.date))

    assets = {item.local_symbol: item.instrument_id for item in asset_binding.assets}
    if all(assets[symbol].instrument_type == 'stock' for symbol in symbols):
        sources = {row['source_ref']: row for row in bundle.auxiliary['stock_sources'].to_dict('records')}
        event_source = sources[bundle.manifest.field_semantics['stock_event_coverage_source']]
        event_start, event_end = (pd.Timestamp(event_source[name]).date()
                                  for name in ('coverage_start', 'coverage_end'))
        usable.intersection_update(day for day in days if event_start <= day <= event_end)
        for symbol in symbols:
            listing = pd.Timestamp(bundle.instruments.loc[bundle.instruments.symbol == symbol,
                                                          'listing_date'].iloc[0]).date()
            usable.difference_update(day for day in days if day < listing)

        def source_covers(row, day):
            source = sources[row['source_ref']]
            opening = pd.Timestamp(datetime.combine(day, time(9, 30), ZoneInfo('Asia/Shanghai')))
            available = pd.Timestamp(row['available_at'])
            return (pd.Timestamp(source['coverage_start']).date() <= day
                    <= pd.Timestamp(source['coverage_end']).date()
                    and pd.Timestamp(source['available_at']) <= available <= opening)

        opening = bundle.auxiliary['stock_open_status'].copy()
        opening['day'] = pd.to_datetime(opening.date).dt.date
        for symbol in symbols:
            member = opening.loc[opening.symbol == symbol]
            counts = member.groupby('day').size()
            unique_days = set(counts[counts == 1].index)
            valid = {row['day'] for row in member.to_dict('records')
                     if row['day'] in unique_days and row['day'] in usable and source_covers(row, row['day'])}
            usable.intersection_update(valid)
        rules = bundle.auxiliary['fee_rules'].to_dict('records')
        for exchange in {assets[symbol].exchange for symbol in symbols}:
            for side in ('buy', 'sell'):
                matching = [row for row in rules if row['exchange'] == exchange and row['side'] == side]
                usable.intersection_update(day for day in days if any(
                    pd.Timestamp(row['effective_from']).date() <= day < pd.Timestamp(row['effective_to']).date()
                    and source_covers(row, day) for row in matching))

    runs, current = [], []
    for day in days:
        if day in usable:
            current.append(day)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return sorted(runs, key=lambda run: (-len(run), run[0]))


def _schedule_report(report, learning, bundle, original, start, end):
    decisions = learning.schedule(bundle.calendar, start, end, original.frequency)
    if len(decisions):
        return report
    check = _blocked('learning_schedule', 'spec', 'DATA_REQUIREMENT_UNSATISFIED',
                     'The interval has no policy decision with a following execution session.')
    return report.model_copy(update={'status': 'blocked', 'checks': (*report.checks, check)})


def _repairable(check):
    if check.name in {'run_coverage', 'requirement_interval', 'calendar_coverage',
                      'trading_status', 'price_observation', 'observation_coverage'}:
        return True
    if check.name == 'stock_execution_evidence':
        return check.message.split(': ', 1)[-1] in {
            'missing stock opening status', 'stock fee coverage gap',
            'stock event evidence does not cover requested interval',
            'stock source does not cover execution date', 'stock evidence unavailable at opening',
            'stock not listed by interval start',
        }
    return False


def preview_backtest(store, model_id, body):
    request = PolicyBacktestRequest.model_validate(body)
    requested = {'start': request.spec.start.isoformat(), 'end': request.spec.end.isoformat()}

    def response(report, available=None, reasons=()):
        if reasons:
            if available is not None:
                explanation = '输入区间无法被所需数据完整覆盖，已找到范围内交易日最多的可回测连续区间。'
            elif any(item.name == 'learning_schedule' for item in report.checks if item.status == 'blocked'):
                explanation = '输入区间内没有自然调仓日及其后续执行交易日，无法回测。'
            else:
                explanation = '所选模型或区间的数据验证未通过，当前无法提供可回测区间。'
            reasons = (explanation, *reasons)
        return {'requested_interval': requested, 'available_interval': available,
                'requires_confirmation': available is not None and any(
                    available[key] != requested[key] for key in ('start', 'end')),
                'report': report.model_dump(mode='json'), 'reasons': list(reasons)}

    resolver = FrameworkResolver(store)
    try:
        with store.transaction() as db:
            owner = db.execute('SELECT model_id FROM investing_sessions WHERE job_id=?',
                               (request.version_id,)).fetchone()
        if owner is None or owner['model_id'] != model_id:
            report = _report('policy_backtest', [_blocked('version_owner', 'model_id',
                'MODEL_IDENTITY_MISMATCH', 'Version does not belong to the requested model.')])
            return response(report, reasons=(report.checks[0].message,))
        binding, original, definitions = resolver._model_binding(model_id)
        dataset = store.get_dataset(request.dataset_id)
        asset_binding = BindingStore(store).get_asset_binding(request.dataset_id)
        bundle = load_dataset(verify_dataset_version(dataset))

        def check(start, end):
            candidate = request.model_copy(update={'spec': request.spec.model_copy(update={'start': start, 'end': end})})
            _, report = resolver._resolve_policy_backtest(candidate, binding, original, definitions,
                dataset_override=dataset, binding_override=asset_binding, bundle_override=bundle)
            return _schedule_report(report, definitions['learning'], bundle, original, start, end)

        report = check(request.spec.start, request.spec.end)
        failures = tuple(item for item in report.checks if item.status == 'blocked')
        reasons = tuple(dict.fromkeys(item.message for item in failures))
        if report.status == 'ready':
            count = sum(request.spec.start <= day <= request.spec.end for day in _days(bundle.calendar))
            return response(report, {**requested, 'trading_sessions': count})
        if any(not _repairable(item) for item in failures):
            return response(report, reasons=reasons)
        runs = _candidate_runs(bundle, asset_binding, original.backtest.symbols,
                               binding.training_configuration.requirements, request.spec.start, request.spec.end)
        for run in runs:
            candidate_report = check(run[0], run[-1])
            if candidate_report.status == 'ready':
                available = {'start': run[0].isoformat(), 'end': run[-1].isoformat(), 'trading_sessions': len(run)}
                return response(candidate_report, available, reasons)
            if any(not _repairable(item) and item.name != 'learning_schedule'
                   for item in candidate_report.checks if item.status == 'blocked'):
                return response(candidate_report, reasons=tuple(dict.fromkeys((*reasons, *(
                    item.message for item in candidate_report.checks if item.status == 'blocked')))))
        return response(report, reasons=reasons)
    except (KeyError, ValueError, OSError, TypeError) as exc:
        code = 'IDENTITY_UNRESOLVED' if isinstance(exc, KeyError) else _model_code(exc)
        report = _report('policy_backtest', [_blocked('model_operation', 'version_id', code, str(exc))])
        return response(report, reasons=(str(exc),))
