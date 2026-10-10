from __future__ import annotations

import json
import hashlib
import sqlite3
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


def test_legacy_learning_submit_rejects_old_job_kind(tmp_path):
    from quanto.learning.service import LearningService
    app = create_app(tmp_path / 'workspace')
    service = LearningService(app.state.service)
    with pytest.raises(ValueError, match='resolved framework configuration'):
        service._submit('prepare_learning', {'spec': {}}, {})
    assert service.store.list_jobs() == []


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


def test_legacy_learning_writes_are_closed(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}
    for path in ('/api/learning/datasets', '/api/learning/train', '/api/learning/backtests'):
        response = client.post(path, headers=headers, json={'spec': {}, 'path': 'C:/private'})
        assert response.status_code == 422
        assert response.json()['code'] == 'MIGRATION_REQUIRED'
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
    for resource in ('datasets', 'models', 'backtests'):
        assert cli.main(['--token', 'test', 'learning', 'get', resource, 'historical']) == 0
        assert json.loads(capsys.readouterr().out) == {'ok': True}
    assert calls == [('GET', f'/learning/{resource}/historical', None)
                     for resource in ('datasets', 'models', 'backtests')]


def _historical_artifact(store, identity, kind, manifest, files):
    root = store.artifact_root / 'historical' / identity
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_bytes(content)
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    with store.transaction() as db:
        db.execute('INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)',
                   (identity, kind, str(root), hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
                    json.dumps(manifest), json.dumps(hashes), '2024-01-01T00:00:00+00:00'))
    return root


def test_historical_learning_artifacts_and_results_remain_readable(tmp_path):
    store = WorkbenchStore(tmp_path / 'workspace')
    _historical_artifact(store, 'old-dataset', 'dataset', {'name': 'old minute dataset'},
                         {'manifest.json': b'{}'})
    _historical_artifact(store, 'old-model', 'model', {'name': 'old minute model'},
                         {'manifest.json': b'{}', 'curves.json': b'[{"epoch": 1}]'})
    result_root = store.artifact_root / 'historical' / 'old-backtest'
    result_root.mkdir(parents=True)
    with sqlite3.connect(result_root / 'results.sqlite3') as db:
        db.execute('CREATE TABLE rows (name TEXT, number INTEGER, payload TEXT, PRIMARY KEY(name,number)) WITHOUT ROWID')
        db.execute('CREATE TABLE counts (name TEXT PRIMARY KEY, count INTEGER)')
        rows = [('equity', number, json.dumps({'timestamp': f'2024-01-01T00:0{number}:00Z',
                                              'equity': str(100 + number), 'drawdown': '0'}))
                for number in range(1, 4)]
        db.executemany('INSERT INTO rows VALUES (?,?,?)', rows)
        db.execute("INSERT INTO counts VALUES ('equity',3)")
    _historical_artifact(store, 'old-backtest', 'backtest', {'counts': {'bars': 3}},
                         {'summary.json': b'{"counts":{"bars":3}}',
                          'results.sqlite3': (result_root / 'results.sqlite3').read_bytes(),
                          'equity.csv': b'timestamp,equity\\n2024-01-01T00:01:00Z,101\\n'})

    app = create_app(store.root)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}
        overview = client.get('/api/learning/overview', headers=headers).json()
        assert [item['id'] for item in overview['datasets']] == ['old-dataset']
        assert [item['id'] for item in overview['models']] == ['old-model']
        assert [item['id'] for item in overview['backtests']] == ['old-backtest']
        for resource, identity in (('datasets', 'old-dataset'), ('models', 'old-model'),
                                   ('backtests', 'old-backtest')):
            assert client.get(f'/api/learning/{resource}', headers=headers).json()['items'][0]['id'] == identity
            assert client.get(f'/api/learning/{resource}/{identity}', headers=headers).status_code == 200
        model = client.get('/api/learning/models/old-model', headers=headers).json()
        assert model['manifest']['history'] == [{'epoch': 1}]
        table = client.get('/api/learning/backtests/old-backtest/tables/equity?limit=2', headers=headers).json()
        assert len(table['items']) == 2 and table['next_cursor'] == '2'
        chart = client.get('/api/learning/backtests/old-backtest/charts?max_points=2', headers=headers).json()
        assert len(chart['points']) == 2 and chart['total_count'] == 3
        export = client.get('/api/learning/backtests/old-backtest/export', headers=headers)
        assert export.status_code == 200 and export.content[:2] == b'PK'
