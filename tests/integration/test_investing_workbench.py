from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from quanto.api.app import create_app
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.framework_imports import fixture_identity_evidence
from tests.integration.test_investing_models import model_workspace


@pytest.fixture(autouse=True)
def isolated_market(tmp_path, monkeypatch):
    config = tmp_path / 'market-config.json'
    config.write_text(json.dumps({'root': str(tmp_path / 'market')}), encoding='utf-8')
    monkeypatch.setenv('QUANTO_MARKET_CONFIG', str(config))


@pytest.mark.parametrize('renamed', [False, True])
def test_minute_jobs_survive_policy_migration(tmp_path, renamed):
    with sqlite3.connect(tmp_path / 'workbench.sqlite3') as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('backtest','demo','fetch','import','validate_strategy','prepare_learning','train','model_backtest')), state TEXT NOT NULL, run_id TEXT UNIQUE, enqueue_seq INTEGER NOT NULL UNIQUE, artifact_root TEXT NOT NULL, pid INTEGER, process_start TEXT, heartbeat TEXT, completed_through TEXT, error TEXT)")
        db.execute("INSERT INTO jobs(id,kind,state,enqueue_seq,artifact_root) VALUES ('old','train','succeeded',1,?)", (str(tmp_path),))
        if renamed:
            # SQLite quotes the resulting table name after the earlier minute migration.
            db.execute('ALTER TABLE jobs RENAME TO jobs_previous')
            db.execute('ALTER TABLE jobs_previous RENAME TO jobs')
    WorkbenchStore(tmp_path)
    store = WorkbenchStore(tmp_path)
    with store.transaction() as db:
        for sequence, kind in enumerate(('train_policy', 'policy_backtest'), 2):
            db.execute("INSERT INTO jobs(id,kind,state,enqueue_seq,artifact_root) VALUES (?,?,'queued',?,?)", (kind, kind, sequence, str(tmp_path)))
        assert db.execute("SELECT state FROM jobs WHERE id='old'").fetchone()[0] == 'succeeded'


def test_policy_overview_auth_and_separate_resources(tmp_path):
    app = create_app(tmp_path / 'workspace')
    client = TestClient(app, base_url='http://127.0.0.1')
    assert client.get('/api/investing/overview').status_code == 401
    headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}
    response = client.get('/api/investing/overview', headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['datasets'] == body['policies'] == body['backtests'] == body['jobs'] == []
    assert body['capabilities']['algorithm'] == 'PPO'
    assert body['capabilities']['frequencies'] == ['weekly', 'monthly']
    assert client.get('/api/investing/models', headers=headers).json()['items'] == []
    assert client.get('/api/investing/policies/missing', headers=headers).status_code == 404
    for route in ('train', 'backtests'):
        response = client.post(f'/api/investing/{route}', headers=headers, json={'dataset_id': 'missing', 'spec': {}, 'path': 'C:/private'})
        assert response.status_code == 422
    assert app.state.service.store.list_jobs() == []


def test_policy_submission_frozen_and_cancel_cannot_publish(tmp_path):
    from quanto.investing.service import InvestingService
    from quanto.learning.registry import finish_learning_job
    from quanto.workbench.scheduler import Scheduler
    app = create_app(tmp_path / 'workspace')
    service = InvestingService(app.state.service)
    submitted = service._submit('train_policy', {'spec': {}}, {})
    record = service.store.get_job(submitted['id'])
    path = Path(record.input_path)
    payload = json.loads(path.read_bytes())
    payload['input']['request']['spec']['name'] = 'changed'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='INPUT_CHANGED'):
        Scheduler(service.store)._launch(record)
    with service.store.transaction() as db:
        db.execute("UPDATE jobs SET state='cancel_requested' WHERE id=?", (record.id,))
    assert not finish_learning_job(service.store, record.id, tmp_path / 'absent', {})
    with service.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM learning_artifacts').fetchone()[0] == 0


def test_standalone_policy_training_requires_model_identity(tmp_path):
    from tests.fixtures.investing_training_cases import investing_case
    spec, _ = investing_case(tmp_path / 'data')
    spec['backtest']['symbols'] = ['SZSE:etf:000001']
    app = create_app(tmp_path / 'workspace')
    source = tmp_path / 'data'
    dataset = app.state.service.store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))
    client = TestClient(app, base_url='http://127.0.0.1')
    headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}
    response = client.post('/api/investing/train', headers=headers, json={'dataset_id': dataset.id, 'spec': spec})
    assert response.status_code == 422
    assert app.state.service.store.list_jobs() == []


def test_supervised_policy_training_backtest_export_and_tamper(model_workspace):
    pytest.importorskip('stable_baselines3')
    from quanto.investing.service import InvestingService
    from quanto.learning.registry import verify_ref
    app, _, _, model_request = model_workspace
    spec = model_request['spec']
    dataset_id = model_request['dataset_id']
    with TestClient(app, base_url='http://127.0.0.1') as client:
        headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}

        def submit(route, body):
            response = client.post(f'/api/investing/{route}', headers=headers, json=body)
            assert response.status_code == 200, response.text
            job = response.json()
            deadline = time.monotonic() + 90
            while job['state'] in {'queued', 'running', 'cancel_requested'} and time.monotonic() < deadline:
                time.sleep(.05)
                job = client.get(f'/api/jobs/{job["id"]}', headers=headers).json()
            if job['state'] != 'succeeded':
                record = app.state.service.store.get_job(job['id'])
                trace = record.artifact_root / record.id / 'control' / 'traceback.txt'
                pytest.fail(f'{job}\n{trace.read_text(encoding="utf-8") if trace.exists() else "no traceback"}')
            return job['id']

        invalid = {**spec, 'validation_start': spec['train_end']}
        assert client.post('/api/investing/models', headers=headers,
                           json={**model_request, 'spec': invalid}).status_code == 422
        model_response = client.post('/api/investing/models', headers=headers, json=model_request)
        assert model_response.status_code == 200, model_response.text
        model_id = model_response.json()['id']
        policy = submit(f'models/{model_id}/train', {'additional_timesteps': spec['total_timesteps']})
        manifest = client.get(f'/api/investing/policies/{policy}', headers=headers).json()['manifest']
        assert manifest['cutoff'] == spec['validation_end'] and manifest['history']
        assert manifest['best_timesteps'] > 0
        assert {'validation', 'test'} <= manifest['metrics'].keys()
        backtest_spec = {'start': spec['test_start'], 'end': spec['test_end'], 'baseline': 'policy',
                         'evaluation_purpose': 'post_selection_backtest'}
        historical_request = {**backtest_spec, 'start': spec['validation_start'], 'end': spec['validation_end'],
                              'evaluation_purpose': 'development_replay'}
        historical = submit(f'models/{model_id}/backtests',
                            {'dataset_id': dataset_id, 'version_id': policy, 'spec': historical_request})
        historical_details = client.get(f'/api/investing/backtests/{historical}', headers=headers).json()['manifest']
        assert historical_details['period'] == {'start': spec['validation_start'], 'end': spec['validation_end']}
        assert historical_details['counts']['equity'] == 20
        backtest = submit(f'models/{model_id}/backtests',
                          {'dataset_id': dataset_id, 'version_id': policy, 'spec': backtest_spec})
        details = client.get(f'/api/investing/backtests/{backtest}', headers=headers).json()['manifest']
        assert details['counts']['equity'] == 30
        assert details['dataset_content_hash'] == app.state.service.store.get_dataset(dataset_id).content_hash
        assert details['policy_id'] == policy
        page = client.get(f'/api/investing/backtests/{backtest}/tables/equity?limit=2', headers=headers).json()
        assert len(page['items']) == 2 and page['next_cursor'] == '2'
        chart = client.get(f'/api/investing/backtests/{backtest}/charts?max_points=5', headers=headers).json()
        assert len(chart['points']) == 5 and chart['total_count'] == 30
        export = client.get(f'/api/investing/backtests/{backtest}/export', headers=headers)
        assert export.status_code == 200 and export.content[:2] == b'PK'
        assert client.get('/api/learning/overview', headers=headers).json()['models'] == []
        assert len(client.get('/api/investing/overview', headers=headers).json()['policies']) == 1
        service = InvestingService(app.state.service)
        ref = service.reference(policy, 'policy')
        path = verify_ref(ref) / 'policy.zip'
        path.write_bytes(path.read_bytes() + b'tamper')
        assert client.post(f'/api/investing/models/{model_id}/backtests', headers=headers, json={
            'dataset_id': dataset_id, 'version_id': policy, 'spec': backtest_spec}).status_code == 422
