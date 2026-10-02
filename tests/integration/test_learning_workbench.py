from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quanto.api.app import create_app
from quanto.workbench.store import WorkbenchStore


@pytest.fixture(autouse=True)
def isolated_market(tmp_path, monkeypatch):
    config = tmp_path / 'market-config.json'
    config.write_text(json.dumps({'root': str(tmp_path / 'market')}), encoding='utf-8')
    monkeypatch.setenv('QUANTO_MARKET_CONFIG', str(config))


def test_existing_jobs_survive_learning_migration(tmp_path):
    # Create the actual previous schema, rather than an already migrated store.
    with sqlite3.connect(tmp_path / 'workbench.sqlite3') as db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('backtest','demo','fetch','import','validate_strategy')), state TEXT NOT NULL, run_id TEXT UNIQUE, enqueue_seq INTEGER NOT NULL UNIQUE, artifact_root TEXT NOT NULL, pid INTEGER, process_start TEXT, heartbeat TEXT, completed_through TEXT, error TEXT)")
        db.execute("INSERT INTO jobs(id,kind,state,enqueue_seq,artifact_root) VALUES ('old','fetch','succeeded',1,?)", (str(tmp_path),))
    store = WorkbenchStore(tmp_path)
    reopened = WorkbenchStore(tmp_path)
    with reopened.transaction() as db:
        db.execute("INSERT INTO jobs(id,kind,state,enqueue_seq,artifact_root) VALUES ('new','train','queued',2,?)", (str(tmp_path),))
        assert db.execute("SELECT state FROM jobs WHERE id='old'").fetchone()[0] == "succeeded"
        assert db.execute("SELECT count(*) FROM learning_artifacts").fetchone()[0] == 0


def test_frozen_learning_input_rejects_changed_request(tmp_path):
    from quanto.learning.service import LearningService
    from quanto.workbench.scheduler import Scheduler
    app = create_app(tmp_path / 'workspace')
    service = LearningService(app.state.service)
    submitted = service._submit('prepare_learning', {'spec': {}}, {})
    record = service.store.get_job(submitted['id'])
    path = Path(record.input_path)
    payload = json.loads(path.read_bytes())
    payload['input']['request']['spec']['name'] = 'changed after queueing'
    path.write_text(json.dumps(payload), encoding='utf-8')
    scheduler = Scheduler(service.store)
    with pytest.raises(ValueError, match='INPUT_CHANGED'):
        scheduler._launch(record)


def test_learning_environment_rejects_dependency_drift():
    from quanto.learning.registry import runtime_versions, verify_runtime
    versions = runtime_versions()
    verify_runtime(versions)
    versions['numpy'] = '0.0.changed'
    with pytest.raises(ValueError, match='ENVIRONMENT_CHANGED'):
        verify_runtime(versions)


def test_learning_publication_cancel_race(tmp_path, monkeypatch):
    from quanto.learning import registry
    store = WorkbenchStore(tmp_path)
    with store.transaction() as db:
        db.execute("INSERT INTO jobs(id,kind,state,enqueue_seq,artifact_root) VALUES ('race','train','running',1,?)", (str(tmp_path),))
    artifact = tmp_path / 'final' / 'artifact'
    artifact.mkdir(parents=True)
    (artifact / 'manifest.json').write_text('{}', encoding='utf-8')
    original = registry.hash_files
    hashes = original(artifact)
    def cancel_during_verification(root):
        with store.transaction() as db:
            db.execute("UPDATE jobs SET state='cancel_requested' WHERE id='race'")
        return original(root)
    monkeypatch.setattr(registry, 'hash_files', cancel_during_verification)
    result = {'artifact_kind': 'model', 'hashes': {f'artifact/{name}': value for name, value in hashes.items()}}
    assert not registry.finish_learning_job(store, 'race', artifact.parent, result)
    with store.transaction() as db:
        assert db.execute('SELECT count(*) FROM learning_artifacts').fetchone()[0] == 0


def test_result_verification_cache_invalidates_on_change(tmp_path):
    import hashlib
    from quanto.learning.service import verify_result_file
    path = tmp_path / 'result'
    path.write_bytes(b'original')
    expected = hashlib.sha256(b'original').hexdigest()
    verify_result_file(path, expected)
    verify_result_file(path, expected)
    path.write_bytes(b'changed-data')
    with pytest.raises(ValueError, match='INVALID_ARTIFACT'):
        verify_result_file(path, expected)


def test_expected_trading_days_use_overlapping_complete_session_bars():
    from quanto.learning.jobs import expected_trading_days
    manifest = {'frequency_minutes': 5, 'trading_sessions': [
        {'trading_date': day, 'start': f'{day}T01:30:00Z', 'end': f'{day}T03:30:00Z'}
        for day in ('2024-01-05', '2024-01-08', '2024-01-09')]}
    assert expected_trading_days(manifest, {'start': '2024-01-05T00:00:00Z', 'end': '2024-01-10T00:00:00Z'}) == ['2024-01-05', '2024-01-08', '2024-01-09']
    assert expected_trading_days(manifest, {'start': '2024-01-08T01:31:00Z', 'end': '2024-01-08T01:40:00Z'}) == ['2024-01-08']
    assert expected_trading_days(manifest, {'start': '2024-01-08T03:29:00Z', 'end': '2024-01-09T01:34:00Z'}) == []


def test_learning_routes_auth_and_empty_overview(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get('/api/learning/overview').status_code == 401
    token = client.get('/api/session').json()['token']
    response = client.get('/api/learning/overview', headers={'Authorization': f'Bearer {token}'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['datasets'] == body['models'] == body['backtests'] == []
    assert {'torch_available', 'cuda_available', 'device_name'} <= body['capabilities'].keys()


def test_learning_submission_rejects_unknown_model_and_host_path(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}
    response = client.post('/api/learning/train', headers=headers, json={'dataset_id': 'missing', 'spec': {}})
    assert response.status_code in (404, 422)
    response = client.post('/api/learning/datasets', headers=headers, json={'spec': {}, 'path': 'C:/private'})
    assert response.status_code == 422
    assert app.state.service.store.list_jobs() == []


def test_cancelled_learning_job_cannot_publish(tmp_path):
    from quanto.learning.registry import finish_learning_job
    store = WorkbenchStore(tmp_path)
    with store.transaction() as db:
        db.execute("INSERT INTO jobs(id,kind,state,enqueue_seq,artifact_root) VALUES ('cancelled','train','cancel_requested',1,?)", (str(tmp_path),))
    assert not finish_learning_job(store, 'cancelled', tmp_path / 'absent', {})
    with store.transaction() as db:
        assert db.execute('SELECT count(*) FROM learning_artifacts').fetchone()[0] == 0


def test_learning_cli_uses_same_api(monkeypatch, tmp_path, capsys):
    from quanto import cli
    calls = []
    monkeypatch.setattr(cli.WorkbenchClient, 'request', lambda self, method, path, body=None: calls.append((method, path, body)) or {'ok': True})
    config = tmp_path / 'train.json'
    config.write_text(json.dumps({'dataset_id': 'd', 'spec': {'name': 'training'}}), encoding='utf-8')
    assert cli.main(['--token', 'test', 'learning', 'train', '--json', str(config)]) == 0
    assert calls == [('POST', '/learning/train', {'dataset_id': 'd', 'spec': {'name': 'training'}})]
    assert json.loads(capsys.readouterr().out) == {'ok': True}


def test_supervised_prepare_train_backtest_and_results(tmp_path):
    pytest.importorskip('torch')
    from tests.fixtures.learning_cases import learning_case, training_case
    prepare, start = learning_case(tmp_path / 'market')
    app = create_app(tmp_path / 'workspace')
    with TestClient(app, base_url='http://127.0.0.1') as client:
        headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}

        def submit(path, body):
            response = client.post(path, headers=headers, json=body)
            assert response.status_code == 200, response.text
            job = response.json()
            deadline = time.monotonic() + 90
            while job['state'] in {'queued', 'running', 'cancel_requested'} and time.monotonic() < deadline:
                time.sleep(.1)
                job = client.get(f'/api/jobs/{job["id"]}', headers=headers).json()
            if job['state'] != 'succeeded':
                record = app.state.service.store.get_job(job['id'])
                trace = record.artifact_root / record.id / 'control' / 'traceback.txt'
                pytest.fail(f'{job}\n{trace.read_text(encoding="utf-8") if trace.exists() else "no traceback"}')
            return job['id']

        dataset = submit('/api/learning/datasets', {'spec': prepare})
        training = training_case(start)
        model = submit('/api/learning/train', {'dataset_id': dataset, 'spec': training})
        spec = {'start': training['test_start'], 'end': training['test_end'], 'baseline': 'model',
                'initial_cash': 10000, 'lot_size': 1, 'settlement_days': 0}
        bad = client.post('/api/learning/backtests', headers=headers,
                          json={'dataset_id': dataset, 'model_id': model, 'spec': {**spec, 'start': training['train_start']}})
        assert bad.status_code == 422
        result = submit('/api/learning/backtests', {'dataset_id': dataset, 'model_id': model, 'spec': spec})
        details = client.get(f'/api/learning/backtests/{result}', headers=headers).json()
        assert details['manifest']['counts']['bars'] == 30
        table = client.get(f'/api/learning/backtests/{result}/tables/equity?limit=2', headers=headers).json()
        assert len(table['items']) == 2 and table['next_cursor'] == '2'
        chart = client.get(f'/api/learning/backtests/{result}/charts?max_points=5', headers=headers).json()
        assert len(chart['points']) == 5 and chart['total_count'] == 30
        export = client.get(f'/api/learning/backtests/{result}/export', headers=headers)
        assert export.status_code == 200 and export.content[:2] == b'PK'
        manifest = client.get(f'/api/learning/models/{model}', headers=headers).json()['manifest']
        assert manifest['cutoff'] == training['validation_end']
        assert manifest['history']
        assert len(client.get('/api/learning/overview', headers=headers).json()['models']) == 1

        # Cancel a real worker after an atomically saved epoch, then resume it.
        long_spec = {**training, 'epochs': 1000, 'patience': 100, 'batch_size': 1}
        interrupted = client.post('/api/learning/train', headers=headers,
                                  json={'dataset_id': dataset, 'spec': long_spec}).json()
        record = app.state.service.store.get_job(interrupted['id'])
        checkpoint = record.artifact_root / record.id / 'staging' / 'artifact' / 'checkpoints' / 'checkpoint.json'
        deadline = time.monotonic() + 60
        while not checkpoint.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert checkpoint.exists()
        response = client.post(f'/api/jobs/{record.id}/cancel', headers=headers, json={})
        assert response.status_code == 200, response.text
        while time.monotonic() < deadline:
            state = client.get(f'/api/jobs/{record.id}', headers=headers).json()['state']
            if state == 'cancelled':
                break
            time.sleep(.05)
        assert state == 'cancelled'
        assert client.get(f'/api/learning/models/{record.id}', headers=headers).status_code == 404
        completed_epoch = json.loads(checkpoint.read_bytes())['epoch']
        resumed = submit('/api/learning/train', {'dataset_id': dataset, 'resume_job_id': record.id,
                                               'spec': {**long_spec, 'epochs': completed_epoch + 1}})
        continued = client.get(f'/api/learning/models/{resumed}', headers=headers).json()['manifest']
        assert continued['epochs_completed'] == completed_epoch + 1
        assert continued['resume_parent']

        # Reproduce cancellation winning after a worker already moved staging to final.
        import shutil
        late_id = 'late-cancel-after-final-move'
        store = app.state.service.store
        original = store.get_job(model)
        late_root = store.artifact_root / late_id / 'final' / 'artifact'
        shutil.copytree(original.artifact_root / model / 'final' / 'artifact', late_root)
        with store.transaction() as db:
            seq = db.execute('SELECT MAX(enqueue_seq)+1 FROM jobs').fetchone()[0]
            db.execute("INSERT INTO jobs(id,kind,state,enqueue_seq,artifact_root,input_path) VALUES (?,'train','cancelled',?,?,?)",
                       (late_id, seq, str(store.artifact_root), original.input_path))
        overview = client.get('/api/learning/overview', headers=headers).json()
        assert next(job for job in overview['jobs'] if job['id'] == late_id)['checkpoint_available']
        late_resume = submit('/api/learning/train', {'dataset_id': dataset, 'resume_job_id': late_id,
                                                    'spec': {**training, 'epochs': 3}})
        assert client.get(f'/api/learning/models/{late_resume}', headers=headers).status_code == 200
        assert client.get(f'/api/learning/models/{late_id}', headers=headers).status_code == 404
