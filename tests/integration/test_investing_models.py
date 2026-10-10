from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quanto.api.app import create_app
from tests.fixtures.investing_training_cases import investing_case
from tests.fixtures.framework_imports import fixture_identity_evidence
from quanto.backtest.trading import ETF_TRADING_DEFINITION
from quanto.investing.definitions import etf_learning_definitions
from quanto.workbench.baskets import BasketStore
from quanto.workbench.bindings import BindingStore


@pytest.fixture
def model_workspace(tmp_path, monkeypatch):
    market_config = tmp_path / 'market.json'
    market_config.write_text(json.dumps({'root': str(tmp_path / 'market')}))
    monkeypatch.setenv('QUANTO_MARKET_CONFIG', str(market_config))
    spec, _ = investing_case(tmp_path / 'data')
    for name in ('prices', 'status', 'instruments'):
        path = tmp_path / 'data' / f'{name}.csv'
        frame = pd.read_csv(path, dtype={'symbol': str})
        pd.concat([frame, frame.assign(symbol='000002')], ignore_index=True).to_csv(path, index=False)
    app = create_app(tmp_path / 'workspace')
    source = tmp_path / 'data'
    dataset = app.state.service.store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))
    del spec['backtest']['symbols']
    binding = BindingStore(app.state.service.store).get_asset_binding(dataset.id)
    basket = BasketStore(app.state.service.store).create('Model basket', (binding.assets[0].instrument_id,),
                                                       'Synthetic fixture', 'model-basket')
    definition = next(item for item in etf_learning_definitions()
                      if item.action_version == 'hold-target-cash-quartiles-v2'
                      and item.reward_version == 'net-log-return-v1')
    client = TestClient(app, base_url='http://127.0.0.1')
    headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}
    return app, client, headers, {'dataset_id': dataset.id, 'spec': spec,
        'framework_contract_version': 'multi-asset-v1', 'basket_revision_ref': basket.ref.model_dump(mode='json'),
        'learning_definition_ref': definition.ref.model_dump(mode='json'),
        'trading_definition_ref': ETF_TRADING_DEFINITION.ref.model_dump(mode='json')}


def test_model_creation_persists_without_training_and_keeps_order(model_workspace):
    app, client, headers, request = model_workspace
    binding = BindingStore(app.state.service.store).get_asset_binding(request['dataset_id'])
    basket = BasketStore(app.state.service.store).revise(request['basket_revision_ref']['basket_id'], 1,
        tuple(item.instrument_id for item in reversed(binding.assets)), 'Order is part of model identity')
    request['basket_revision_ref'] = basket.ref.model_dump(mode='json')
    response = client.post('/api/investing/models', headers=headers, json=request)
    assert response.status_code == 200, response.text
    model = response.json()
    assert model['state'] == 'untrained'
    assert model['spec']['backtest']['symbols'] == ['SZSE:etf:000002', 'SZSE:etf:000001']
    assert model['spec']['action_version'] == 'hold-target-cash-quartiles-v2'
    assert model['total_timesteps'] == model['session_count'] == model['version_count'] == 0
    assert model['latest_version_id'] is None
    assert app.state.service.store.list_jobs() == []
    assert client.get('/api/investing/models').status_code == 401
    assert client.get('/api/investing/models', headers=headers).json()['items'][0]['id'] == model['id']
    detail = client.get(f'/api/investing/models/{model["id"]}', headers=headers).json()
    assert detail['sessions'] == detail['versions'] == []
    assert detail['framework_binding']['origin'] == 'new'
    assert detail['framework_binding']['ordered_pairs'][0]['local_symbol'] == 'SZSE:etf:000002'
    reopened = create_app(app.state.service.store.root)
    with reopened.state.service.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM investing_models').fetchone()[0] == 1


def test_model_creation_accepts_dsr_without_test_dates(model_workspace):
    app, client, headers, request = model_workspace
    request['spec'].update(reward_version='differential-sharpe-v1', test_start=None, test_end=None)
    request['learning_definition_ref'] = next(item.ref.model_dump(mode='json') for item in etf_learning_definitions()
        if item.action_version == 'hold-target-cash-quartiles-v2' and item.reward_version == 'differential-sharpe-v1')
    request['spec']['backtest']['end'] = request['spec']['validation_end']
    response = client.post('/api/investing/models', headers=headers, json=request)
    assert response.status_code == 200, response.text
    model = response.json()
    assert model['spec']['reward_version'] == 'differential-sharpe-v1'
    assert model['spec']['test_start'] is None and model['spec']['test_end'] is None
    assert model['state'] == 'untrained'
    assert app.state.service.store.list_jobs() == []


def test_existing_model_without_action_version_requires_audited_binding(model_workspace):
    app, client, headers, request = model_workspace
    from quanto.investing.contracts import PolicyTrainingSpec
    from quanto.workbench.store import utc_now
    historical_spec = PolicyTrainingSpec.model_validate({**request['spec'], 'backtest': {
        **request['spec']['backtest'], 'symbols': ['SZSE:etf:000001']}}).model_dump(mode='json', exclude={'action_version'})
    with app.state.service.store.transaction() as db:
        db.execute('INSERT INTO investing_models VALUES (?,?,?,?)',
                   ('legacy-model', request['dataset_id'], json.dumps(historical_spec), utc_now()))
    assert client.post('/api/investing/models/legacy-model/train', headers=headers,
                       json={'additional_timesteps': 8}).status_code == 422
    BindingStore(app.state.service.store).bind_model('legacy-model', 'audited-legacy')
    response = client.post('/api/investing/models/legacy-model/train', headers=headers,
                           json={'additional_timesteps': 8})
    assert response.status_code == 200, response.text
    record = app.state.service.store.get_job(response.json()['id'])
    payload = json.loads(Path(record.input_path).read_text(encoding='utf-8'))
    assert payload['input']['request']['spec']['action_version'] == 'hold-target-quartiles-v1'


def test_create_model_and_binding_publish_in_one_transaction(model_workspace, monkeypatch):
    app, client, headers, request = model_workspace
    def fail(*args, **kwargs):
        raise ValueError('binding publication failed')
    monkeypatch.setattr(BindingStore, '_insert_model_binding', fail)
    assert client.post('/api/investing/models', headers=headers, json=request).status_code == 422
    with app.state.service.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM investing_models').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM model_framework_bindings').fetchone()[0] == 0


def test_create_rejects_invalid_basket_and_bad_research_without_queueing(model_workspace):
    app, client, headers, request = model_workspace
    for symbols in ([], ['SZSE:etf:000001', 'SZSE:etf:000001'], ['MISSING']):
        invalid = {**request, 'spec': {**request['spec'], 'backtest': {**request['spec']['backtest'], 'symbols': symbols}}}
        assert client.post('/api/investing/models', headers=headers, json=invalid).status_code == 422
    invalid = {**request, 'spec': {**request['spec'], 'episode_sessions': 5000}}
    assert client.post('/api/investing/models', headers=headers, json=invalid).status_code == 422
    assert client.get('/api/investing/models', headers=headers).json()['items'] == []
    assert app.state.service.store.list_jobs() == []


def test_only_one_active_session_per_model_and_no_identity_override(model_workspace):
    app, client, headers, request = model_workspace
    model = client.post('/api/investing/models', headers=headers, json=request).json()
    endpoint = f'/api/investing/models/{model["id"]}/train'
    assert client.post(endpoint, headers=headers, json={'additional_timesteps': 8, 'spec': request['spec']}).status_code == 422
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(endpoint, headers=headers, json={'additional_timesteps': 8, 'device': 'cpu'}), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()['code'] == 'MODEL_TRAINING_CONFLICT'
    job = next(response.json() for response in responses if response.status_code == 200)
    assert job['state'] == 'queued'
    assert len(app.state.service.store.list_jobs()) == 1
    detail = client.get(f'/api/investing/models/{model["id"]}', headers=headers).json()
    assert detail['active_job_id'] == job['id']
    assert detail['sessions'][0]['base_version_id'] is None
    assert client.post(f'/api/jobs/{job["id"]}/cancel', headers=headers, json={}).status_code == 200
    detail = client.get(f'/api/investing/models/{model["id"]}', headers=headers).json()
    assert detail['version_count'] == 0 and detail['active_job_id'] is None
    assert client.post(endpoint, headers=headers, json={'additional_timesteps': 8}).status_code == 200


@pytest.mark.parametrize('budget', [True, 0, -1, 1.5, '16', 10000001])
def test_session_budget_is_strict(model_workspace, budget):
    app, client, headers, request = model_workspace
    model = client.post('/api/investing/models', headers=headers, json=request).json()
    assert client.post(f'/api/investing/models/{model["id"]}/train', headers=headers, json={'additional_timesteps': budget}).status_code == 422
    assert app.state.service.store.list_jobs() == []


def test_model_continues_and_backtests_explicit_immutable_version(model_workspace):
    pytest.importorskip('stable_baselines3')
    app, client, headers, request = model_workspace
    model = client.post('/api/investing/models', headers=headers, json=request).json()
    other = client.post('/api/investing/models', headers=headers, json=request).json()

    def wait_job(response, expected='succeeded'):
        assert response.status_code == 200, response.text
        job = response.json()
        deadline = time.monotonic() + 90
        while job['state'] in {'queued', 'running', 'cancel_requested'} and time.monotonic() < deadline:
            time.sleep(.05)
            job = client.get(f'/api/jobs/{job["id"]}', headers=headers).json()
        if job['state'] != expected:
            record = app.state.service.store.get_job(job['id'])
            trace = record.artifact_root / job['id'] / 'control' / 'traceback.txt'
            pytest.fail(f'{job}\n{trace.read_text(encoding="utf-8") if trace.exists() else "no traceback"}')
        return job['id']

    with client:
        first = wait_job(client.post(f'/api/investing/models/{model["id"]}/train', headers=headers,
                                    json={'additional_timesteps': 8, 'device': 'cpu'}))
        with app.state.service.store.transaction() as db:
            first_root = Path(db.execute('SELECT path FROM learning_artifacts WHERE id=?', (first,)).fetchone()[0])
        from stable_baselines3 import PPO
        from quanto.learning.artifacts import read_manifest
        assert read_manifest(first_root, 'policy')['action_version'] == 'hold-target-cash-quartiles-v2'
        assert PPO.load(first_root / 'last.zip', device='cpu').action_space.nvec[0] == 3
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first_root.iterdir() if path.is_file()}
        second = wait_job(client.post(f'/api/investing/models/{model["id"]}/train', headers=headers,
                                     json={'additional_timesteps': 8, 'device': 'cpu'}))
        detail = client.get(f'/api/investing/models/{model["id"]}', headers=headers).json()
        assert detail['session_count'] == detail['version_count'] == 2
        assert detail['total_timesteps'] == 16
        assert detail['latest_version_id'] == second
        assert detail['sessions'][0]['base_version_id'] == first
        assert detail['versions'][0]['manifest']['timesteps_started'] == 8
        assert detail['versions'][0]['manifest']['metrics']['test'] is None
        history = client.get(f'/api/investing/models/{model["id"]}/history', headers=headers).json()['items']
        for version in detail['versions']:
            sampling = version['manifest']['training_sampling']
            intervals = version['manifest']['training_sampling_intervals']
            assert sampling['episode_count'] == len(intervals) > 0
            record = next(item for item in history if item['id'] == version['id'])
            assert record['training_sampling'] == sampling
            assert 'training_sampling_intervals' not in record
        assert before == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first_root.iterdir() if path.is_file()}
        interrupted = client.post(f'/api/investing/models/{model["id"]}/train', headers=headers,
                                  json={'additional_timesteps': 100000, 'device': 'cpu'})
        assert interrupted.status_code == 200
        interrupted_id = interrupted.json()['id']
        deadline = time.monotonic() + 15
        while client.get(f'/api/jobs/{interrupted_id}', headers=headers).json()['state'] == 'queued' and time.monotonic() < deadline:
            time.sleep(.05)
        assert client.get(f'/api/jobs/{interrupted_id}', headers=headers).json()['state'] == 'running'
        wait_job(client.post(f'/api/jobs/{interrupted_id}/cancel', headers=headers, json={}), expected='cancelled')
        cancelled_detail = client.get(f'/api/investing/models/{model["id"]}', headers=headers).json()
        assert cancelled_detail['latest_version_id'] == second
        assert cancelled_detail['total_timesteps'] == 16 and cancelled_detail['version_count'] == 2
        assert before == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first_root.iterdir() if path.is_file()}
        third = wait_job(client.post(f'/api/investing/models/{model["id"]}/train', headers=headers,
                                    json={'additional_timesteps': 8, 'device': 'cpu'}))
        continued = client.get(f'/api/investing/models/{model["id"]}', headers=headers).json()
        assert continued['latest_version_id'] == third and continued['total_timesteps'] == 24
        assert continued['sessions'][0]['base_version_id'] == second
        backtest = {'dataset_id': request['dataset_id'], 'version_id': first,
                    'spec': {'start': request['spec']['validation_start'], 'end': request['spec']['validation_end'],
                             'baseline': 'policy', 'evaluation_purpose': 'development_replay'}}
        assert client.post(f'/api/investing/models/{other["id"]}/backtests', headers=headers, json=backtest).status_code == 422
        assert client.post(f'/api/investing/models/{model["id"]}/backtests', headers=headers,
                           json={key: value for key, value in backtest.items() if key != 'version_id'}).status_code == 422
        run = wait_job(client.post(f'/api/investing/models/{model["id"]}/backtests', headers=headers, json=backtest))
        summary = client.get(f'/api/investing/backtests/{run}', headers=headers).json()['manifest']
        assert summary['policy_id'] == first and summary['model_id'] == model['id']
        assert summary['period'] == {'start': request['spec']['validation_start'], 'end': request['spec']['validation_end']}
        assert summary['counts']['equity'] == 20 and 'sharpe' in summary['metrics']
        catalog = client.get('/api/backtests/catalog', headers=headers).json()['items']
        assert len(catalog) == 1 and catalog[0]['job_id'] == run
        assert client.get(f'/api/investing/backtests/{run}/export', headers=headers).content[:2] == b'PK'


def test_agent_model_commands_share_http_contract(monkeypatch, tmp_path, capsys):
    from quanto import cli
    calls = []
    monkeypatch.setattr(cli.WorkbenchClient, 'request', lambda self, method, path, body=None: calls.append((method, path, body)) or {'ok': True})
    path = tmp_path / 'request.json'
    path.write_text('{"additional_timesteps":32}')
    for args in (['models'], ['create-model', '--json', str(path)], ['get', 'models', 'm'],
                 ['train-model', 'm', '--json', str(path)], ['model-backtest', 'm', '--json', str(path)]):
        assert cli.main(['--token', 'test', 'investing', *args]) == 0
    assert [call[:2] for call in calls] == [('GET', '/investing/models'), ('POST', '/investing/models'),
        ('GET', '/investing/models/m'), ('POST', '/investing/models/m/train'), ('POST', '/investing/models/m/backtests')]
    capsys.readouterr()
