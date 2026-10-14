"""Date previews use frozen policy evidence without creating a backtest job."""
import json

import pandas as pd
import pytest

from quanto.framework import thaw_json
from quanto.investing.evaluation import policy_identity
from quanto.investing.feature_runtime import learning_runtime_for
from quanto.investing.policy_versions import PolicyVersionStore
from quanto.learning.artifacts import write_manifest
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from quanto.workbench.bindings import BindingStore
from quanto.workbench.store import utc_now
from tests.fixtures.stock_training_cases import stock_identity_evidence
from tests.integration.test_stock_training import stock_workspace


@pytest.fixture
def preview_case(tmp_path, monkeypatch):
    app, client, headers, create = stock_workspace(tmp_path, monkeypatch)
    response = client.post('/api/investing/models', headers=headers, json=create)
    assert response.status_code == 200, response.text
    model_id = response.json()['id']
    store = app.state.service.store
    binding = BindingStore(store).get_model_binding(model_id)
    spec = thaw_json(binding.original_spec)
    from quanto.investing.contracts import PolicyTrainingSpec
    config = PolicyTrainingSpec.model_validate(spec)
    artifact = tmp_path / 'policy'
    artifact.mkdir()
    (artifact / 'policy.zip').write_bytes(b'synthetic saved weights; never loaded')
    manifest = write_manifest(artifact, {
        'kind': 'policy', 'version': 1, 'config': spec, 'model_id': model_id,
        'dataset_content_hash': binding.asset_binding.dataset_content_hash,
        'basket': spec['backtest']['symbols'], 'price_mode': 'raw_cash',
        'framework_learning': learning_runtime_for(binding, binding.training_configuration).identity(config),
        **policy_identity(config),
    })
    files = hash_files(artifact)
    with store.transaction() as db:
        db.execute("INSERT INTO jobs (id,kind,state,enqueue_seq,artifact_root,created_at) "
                   "VALUES ('saved','train_policy','succeeded',1,?,?)", (str(artifact), utc_now()))
        db.execute('INSERT INTO investing_sessions VALUES (?,?,NULL)', ('saved', model_id))
        db.execute('INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)',
                   ('saved', 'policy', str(artifact), digest(json_bytes(files)),
                    json_bytes(manifest).decode(), json_bytes(files).decode(), utc_now()))
        versions = PolicyVersionStore(store)
        versions.insert(db, 'saved', versions.context_for(binding, manifest, 'saved'))
    dates = pd.bdate_range('2024-01-01', periods=100)
    source = tmp_path / 'stock-data'

    def preview(start=0, end=99, *, mutate=None, model=model_id):
        if mutate:
            mutate(source, dates)
        dataset = store.register_dataset(source, identity_evidence=stock_identity_evidence(source))
        body = {'version_id': 'saved', 'dataset_id': dataset.id,
                'spec': {'start': str(dates[start].date()) if isinstance(start, int) else start,
                         'end': str(dates[end].date()) if isinstance(end, int) else end}}
        before = {str(path.relative_to(store.root)): digest(path.read_bytes())
                  for path in store.root.rglob('*') if path.is_file()}
        response = client.post(f'/api/investing/models/{model}/backtests/preview', headers=headers, json=body)
        after = {str(path.relative_to(store.root)): digest(path.read_bytes())
                 for path in store.root.rglob('*') if path.is_file()}
        assert before == after
        assert len(store.list_jobs()) == 1
        return response, dates

    return preview


def _drop_rows(table, positions):
    def mutate(source, dates):
        path = source / f'{table}.csv'
        frame = pd.read_csv(path, dtype={'symbol': str})
        frame.loc[~pd.to_datetime(frame.date).isin(dates[positions])].to_csv(path, index=False)
    return mutate


def test_preview_trims_missing_opening_prefix_and_verifies_candidate(preview_case):
    response, dates = preview_case(mutate=_drop_rows('stock_open_status', slice(0, 20)))
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['requested_interval'] == {'start': '2024-01-01', 'end': str(dates[99].date())}
    assert value['available_interval'] == {'start': str(dates[20].date()), 'end': str(dates[99].date()),
                                           'trading_sessions': 80}
    assert value['requires_confirmation'] is True
    assert value['report']['status'] == 'ready'
    assert any('opening' in reason for reason in value['reasons'])


def test_preview_selects_longest_run_across_internal_opening_gap(preview_case):
    response, dates = preview_case(mutate=_drop_rows('stock_open_status', [35]))
    value = response.json()
    assert value['available_interval'] == {'start': str(dates[36].date()), 'end': str(dates[99].date()),
                                           'trading_sessions': 64}
    assert value['report']['status'] == 'ready'


def test_preview_equal_session_counts_choose_earlier_run(preview_case):
    response, dates = preview_case(end=98, mutate=_drop_rows('stock_open_status', [49]))
    assert response.json()['available_interval'] == {
        'start': str(dates[0].date()), 'end': str(dates[48].date()), 'trading_sessions': 49}


def test_preview_keeps_valid_weekend_bounds(preview_case):
    response, _ = preview_case(start='2024-01-06', end='2024-02-04')
    value = response.json()
    assert value['report']['status'] == 'ready'
    assert value['requires_confirmation'] is False
    assert value['available_interval']['start'] == '2024-01-06'
    assert value['available_interval']['end'] == '2024-02-04'


@pytest.mark.parametrize('start,end', [('2023-01-01', '2023-12-31'), ('2024-01-02', '2024-01-03')])
def test_preview_rejects_no_overlap_or_no_executable_policy_decision(preview_case, start, end):
    response, _ = preview_case(start=start, end=end)
    value = response.json()
    assert value['available_interval'] is None
    assert value['requires_confirmation'] is False
    assert value['report']['status'] == 'blocked'


def test_preview_adjustment_stays_inside_entered_bounds(preview_case):
    response, dates = preview_case(start='2023-01-01', end=25)
    assert response.json()['available_interval'] == {
        'start': str(dates[0].date()), 'end': str(dates[25].date()), 'trading_sessions': 26}


def test_preview_does_not_trim_around_corrupt_source_evidence(preview_case):
    def corrupt(source, dates):
        path = source / 'stock_sources.csv'
        frame = pd.read_csv(path)
        frame['payload_sha256'] = '0' * 64
        frame.to_csv(path, index=False)
    response, _ = preview_case(mutate=corrupt)
    value = response.json()
    assert value['report']['status'] == 'blocked'
    assert value['available_interval'] is None
    assert any('digest' in reason for reason in value['reasons'])


def test_preview_checks_model_version_ownership(preview_case):
    response, _ = preview_case(model='another-model')
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['available_interval'] is None
    assert value['report']['status'] == 'blocked'
    assert any(check['code'] == 'MODEL_IDENTITY_MISMATCH' for check in value['report']['checks'])


def test_preview_honors_exclusive_fee_end(preview_case):
    def shorten_fees(source, dates):
        path = source / 'fee_rules.csv'
        frame = pd.read_csv(path)
        frame['effective_to'] = str(dates[40].date())
        frame.to_csv(path, index=False)
    response, dates = preview_case(mutate=shorten_fees)
    assert response.json()['available_interval'] == {
        'start': str(dates[0].date()), 'end': str(dates[39].date()), 'trading_sessions': 40}


def test_preview_honors_stock_event_source_coverage(preview_case):
    def shorten_sources(source, dates):
        path = source / 'stock_sources.csv'
        frame = pd.read_csv(path)
        frame['coverage_start'] = str(dates[20].date())
        frame['coverage_end'] = str(dates[59].date())
        frame.to_csv(path, index=False)
    response, dates = preview_case(mutate=shorten_sources)
    assert response.json()['available_interval'] == {
        'start': str(dates[20].date()), 'end': str(dates[59].date()), 'trading_sessions': 40}


def test_preview_splits_at_opening_evidence_unavailable_by_open(preview_case):
    def late_opening(source, dates):
        path = source / 'stock_open_status.csv'
        frame = pd.read_csv(path, dtype={'symbol': str})
        frame.loc[35, 'available_at'] = f'{dates[35].date()}T09:31:00+08:00'
        frame.to_csv(path, index=False)
    response, dates = preview_case(mutate=late_opening)
    assert response.json()['available_interval'] == {
        'start': str(dates[36].date()), 'end': str(dates[99].date()), 'trading_sessions': 64}
