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
    dataset = app.state.service.store.register_dataset(tmp_path / 'data')
    client = TestClient(app, base_url='http://127.0.0.1')
    headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}
    return app, client, headers, {'dataset_id': dataset.id, 'spec': spec}


def test_model_creation_persists_without_training_and_keeps_order(model_workspace):
    app, client, headers, request = model_workspace
    request['spec']['backtest']['symbols'] = ['000002', '000001']
    response = client.post('/api/investing/models', headers=headers, json=request)
    assert response.status_code == 200, response.text
    model = response.json()
    assert model['state'] == 'untrained'
    assert model['spec']['backtest']['symbols'] == ['000002', '000001']
    assert model['total_timesteps'] == model['session_count'] == model['version_count'] == 0
    assert model['latest_version_id'] is None
    assert app.state.service.store.list_jobs() == []
    assert client.get('/api/investing/models').status_code == 401
    assert client.get('/api/investing/models', headers=headers).json()['items'][0]['id'] == model['id']
    detail = client.get(f'/api/investing/models/{model["id"]}', headers=headers).json()
    assert detail['sessions'] == detail['versions'] == []
    reopened = create_app(app.state.service.store.root)
    with reopened.state.service.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM investing_models').fetchone()[0] == 1


def test_create_rejects_invalid_basket_and_bad_research_without_queueing(model_workspace):
    app, client, headers, request = model_workspace
    for symbols in ([], ['000001', '000001'], ['MISSING']):
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

    def wait_job(response):
        assert response.status_code == 200, response.text
        job = response.json()
        deadline = time.monotonic() + 90
        while job['state'] in {'queued', 'running', 'cancel_requested'} and time.monotonic() < deadline:
            time.sleep(.05)
            job = client.get(f'/api/jobs/{job["id"]}', headers=headers).json()
        if job['state'] != 'succeeded':
            record = app.state.service.store.get_job(job['id'])
            trace = record.artifact_root / job['id'] / 'control' / 'traceback.txt'
            pytest.fail(f'{job}\n{trace.read_text(encoding="utf-8") if trace.exists() else "no traceback"}')
        return job['id']

    with client:
        first = wait_job(client.post(f'/api/investing/models/{model["id"]}/train', headers=headers,
                                    json={'additional_timesteps': 8, 'device': 'cpu'}))
        with app.state.service.store.transaction() as db:
            first_root = Path(db.execute('SELECT path FROM learning_artifacts WHERE id=?', (first,)).fetchone()[0])
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
        assert before == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first_root.iterdir() if path.is_file()}
        backtest = {'dataset_id': request['dataset_id'], 'version_id': first,
                    'spec': {'start': request['spec']['test_start'], 'end': request['spec']['test_end'], 'baseline': 'policy'}}
        assert client.post(f'/api/investing/models/{other["id"]}/backtests', headers=headers, json=backtest).status_code == 422
        assert client.post(f'/api/investing/models/{model["id"]}/backtests', headers=headers,
                           json={key: value for key, value in backtest.items() if key != 'version_id'}).status_code == 422
        run = wait_job(client.post(f'/api/investing/models/{model["id"]}/backtests', headers=headers, json=backtest))
        summary = client.get(f'/api/investing/backtests/{run}', headers=headers).json()['manifest']
        assert summary['policy_id'] == first and summary['model_id'] == model['id']
        assert summary['counts']['equity'] == 30 and 'sharpe' in summary['metrics']
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
