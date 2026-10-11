from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quanto.api.app import create_app


@pytest.fixture
def history_workspace(tmp_path, monkeypatch):
    config = tmp_path / 'market.json'
    config.write_text(json.dumps({'root': str(tmp_path / 'market')}))
    monkeypatch.setenv('QUANTO_MARKET_CONFIG', str(config))
    from quanto.investing.models import InvestingModels
    from tests.integration.test_framework_capabilities import _training_case
    store, request, _ = _training_case(tmp_path)
    app = create_app(store.root)
    monkeypatch.setattr('quanto.investing.models.uuid4', lambda: 'm1')
    InvestingModels(app.state.service).create_model(request.model_dump(mode='json'))
    client = TestClient(app, base_url='http://127.0.0.1')
    headers = {'Authorization': f'Bearer {client.get("/api/session").json()["token"]}'}
    return app.state.service.store, client, headers


def add_sessions(store, states, start=1, base=None):
    with store.transaction() as db:
        for number, state in enumerate(states, start):
            identity = f'job-{number}'
            db.execute('''INSERT INTO jobs (id,kind,state,enqueue_seq,artifact_root,created_at)
                VALUES (?,'train_policy',?,?,?,?)''',
                       (identity, state, number * 2, str(store.artifact_root), '2026-01-01'))
            db.execute('INSERT INTO investing_sessions VALUES (?,?,?)', (identity, 'm1', base))
            if state == 'succeeded':
                manifest = dict(id=f'content-{number}', timesteps_completed=number * 512,
                    session_timesteps=512, best_timesteps=min(number, 244) * 512,
                    best_source_content_id='content-244' if number > 244 else None,
                    selection_valid=True, runtime={'device': 'cuda'},
                    metrics={'validation': {'sharpe': 2.1}},
                    history=[{'timesteps': number * 512, 'validation_sharpe': 1.5}])
                db.execute('INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)',
                           (identity, 'policy', str(store.artifact_root / identity), f'content-{number}',
                            json.dumps(manifest), '{}', '2026-01-01'))
                base = identity


def test_paginated_versions_locate_rounds_and_return_only_summaries(history_workspace):
    store, client, headers = history_workspace
    add_sessions(store, ['succeeded'] * 305)
    endpoint = '/api/investing/models/m1/history'
    response = client.get(endpoint, headers=headers)
    assert response.status_code == 200, response.text
    page = response.json()
    assert (page['total'], page['page'], page['page_size'], page['total_pages']) == (305, 1, 10, 31)
    assert [item['round_number'] for item in page['items']] == list(range(305, 295, -1))
    assert page['items'][0]['best_round_number'] == 244
    assert page['items'][0]['best_source_version_id'] == 'job-244'
    assert page['items'][0]['validation_latest'] == {'sharpe': 1.5}
    assert all('manifest' not in item and 'history' not in item for item in page['items'])
    for number, expected_page in ((53, 26), (244, 7)):
        located = client.get(endpoint, headers=headers, params={'round_number': number}).json()
        assert located['page'] == expected_page
        assert number in [item['round_number'] for item in located['items']]
    last = client.get(endpoint, headers=headers, params={'page': 31}).json()
    assert [item['round_number'] for item in last['items']] == [5, 4, 3, 2, 1]
    assert client.get(endpoint).status_code == 401
    assert client.get('/api/investing/models/missing/history', headers=headers).status_code == 404


def test_attempt_numbers_do_not_consume_success_rounds_and_anchor_prevents_page_drift(history_workspace):
    store, client, headers = history_workspace
    add_sessions(store, ['succeeded', 'failed', 'succeeded', 'cancelled', 'interrupted', 'running'])
    endpoint = '/api/investing/models/m1/history'
    versions = client.get(endpoint, headers=headers).json()
    assert [item['round_number'] for item in versions['items']] == [2, 1]
    attempts = client.get(endpoint, headers=headers, params={'kind': 'sessions'}).json()
    assert [item['session_number'] for item in attempts['items']] == [6, 5, 4, 3, 2, 1]
    assert [item['round_number'] for item in attempts['items']] == [None, None, None, 2, None, 1]
    assert attempts['items'][0]['base_round_number'] == 2
    located = client.get(endpoint, headers=headers, params={'kind': 'sessions', 'page_size': 2, 'session_number': 3}).json()
    assert located['page'] == 2 and located['items'][1]['id'] == 'job-3'
    add_sessions(store, ['succeeded'], start=7, base='job-3')
    frozen = client.get(endpoint, headers=headers, params={'anchor': versions['anchor']}).json()
    assert frozen['items'] == versions['items'] and frozen['total'] == 2
    assert client.get(endpoint, headers=headers).json()['total'] == 3


def test_lightweight_summary_includes_active_state_without_manifests(history_workspace):
    store, client, headers = history_workspace
    add_sessions(store, ['succeeded', 'running'])
    summary = client.get('/api/investing/models/m1?include_history=false', headers=headers).json()
    assert summary['session_count'] == 2 and summary['version_count'] == 1
    assert summary['latest_version_id'] == 'job-1' and summary['total_timesteps'] == 512
    assert summary['active_session']['id'] == 'job-2'
    assert summary['active_session']['session_number'] == 2
    assert summary['active_session']['training_sampling'] is None
    assert isinstance(summary['capabilities']['cuda_available'], bool)
    assert 'versions' not in summary and 'sessions' not in summary


@pytest.mark.parametrize('params', [
    {'kind': 'unknown'}, {'page': 0}, {'page_size': 0}, {'page_size': 51},
    {'anchor': -1}, {'round_number': 0}, {'round_number': 5},
    {'session_number': 1}, {'kind': 'sessions', 'round_number': 1, 'session_number': 1},
])
def test_history_rejects_invalid_navigation(history_workspace, params):
    _, client, headers = history_workspace
    response = client.get('/api/investing/models/m1/history', headers=headers, params=params)
    assert response.status_code == 422


def test_empty_history_has_no_fake_rounds(history_workspace):
    _, client, headers = history_workspace
    page = client.get('/api/investing/models/m1/history', headers=headers).json()
    assert page == dict(items=[], total=0, page=1, page_size=10, total_pages=1, anchor=0)


def test_history_sampling_is_specific_to_each_session_and_unknown_for_old_versions(history_workspace):
    store, client, headers = history_workspace
    add_sessions(store, ['succeeded'] * 3)
    summaries = [
        {'start': '2024-02-01', 'end': '2024-02-16', 'episode_count': 2},
        {'start': '2024-05-06', 'end': '2024-05-24', 'episode_count': 3},
    ]
    with store.transaction() as db:
        for number, sampling in enumerate(summaries, 1):
            db.execute('''UPDATE learning_artifacts SET manifest_json=json_set(manifest_json,
                '$.training_sampling', json(?), '$.training_sampling_intervals', json(?)) WHERE id=?''',
                       (json.dumps(sampling), json.dumps([{'start': sampling['start'], 'end': sampling['end']}]),
                        f'job-{number}'))
        db.execute('''UPDATE learning_artifacts SET manifest_json=json_set(manifest_json,
            '$.config.training.start', '2020-01-01', '$.config.training.end', '2025-12-31',
            '$.best_source_content_id', 'content-2') WHERE id='job-3' ''')
    old_control = store.artifact_root / 'job-3' / 'control'
    old_control.mkdir(parents=True)
    (old_control / 'training-sampling.json').write_text(json.dumps(summaries[0]), encoding='utf-8')
    page = client.get('/api/investing/models/m1/history', headers=headers).json()
    assert [item['training_sampling'] for item in page['items']] == [None, summaries[1], summaries[0]]
    assert all('training_sampling_intervals' not in item for item in page['items'])


def test_unpublished_sampling_reads_only_visible_sessions_and_active_job_root(history_workspace, tmp_path, monkeypatch):
    store, client, headers = history_workspace
    add_sessions(store, ['failed', 'cancelled', 'running'])
    previous_root = tmp_path / 'previous-artifacts'
    with store.transaction() as db:
        db.execute('UPDATE jobs SET artifact_root=? WHERE id=?', (str(previous_root), 'job-3'))
    summaries = [
        {'start': '2024-02-01', 'end': '2024-02-16', 'episode_count': 1},
        {'start': '2024-03-01', 'end': '2024-03-22', 'episode_count': 2},
        {'start': '2024-05-06', 'end': '2024-05-24', 'episode_count': 3},
    ]
    for number, sampling in enumerate(summaries, 1):
        root = previous_root if number == 3 else store.artifact_root
        control = root / f'job-{number}' / 'control'
        control.mkdir(parents=True)
        (control / 'training-sampling.json').write_text(json.dumps(sampling), encoding='utf-8')
    reads = []
    original = Path.read_text
    def read_text(path, *args, **kwargs):
        if path.name == 'training-sampling.json':
            reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_text)
    endpoint = '/api/investing/models/m1/history'
    page = client.get(endpoint, headers=headers, params={'kind': 'sessions', 'page_size': 1, 'page': 2}).json()
    assert page['items'][0]['training_sampling'] == summaries[1]
    assert [path.parent.parent.name for path in reads] == ['job-2']
    reads.clear()
    summary = client.get('/api/investing/models/m1?include_history=false', headers=headers).json()
    assert summary['active_session']['training_sampling'] == summaries[2]
    assert reads == [previous_root / 'job-3' / 'control' / 'training-sampling.json']
    all_attempts = client.get(endpoint, headers=headers, params={'kind': 'sessions'}).json()
    assert [item['training_sampling'] for item in all_attempts['items']] == summaries[::-1]
    assert all('artifact_root' not in item for item in all_attempts['items'])


@pytest.mark.parametrize('outcome', ['succeeded', 'failed', 'cancelled'])
def test_policy_job_preserves_latest_sampling_summary_before_publication(tmp_path, monkeypatch, outcome):
    from hashlib import sha256
    from quanto.backtest.models import BacktestCancelled
    from quanto.investing import jobs as policy_jobs
    from quanto.learning import jobs
    from quanto.learning.contracts import LearningCancelled

    control = tmp_path / 'job' / 'control'
    control.mkdir(parents=True)
    monkeypatch.setattr(jobs, 'verify_runtime', lambda _: None)
    sampling = {'start': '2024-05-06', 'end': '2024-05-24', 'episode_count': 3}
    source_hashes = {'quanto/investing/jobs.py': sha256(Path(policy_jobs.__file__).read_bytes()).hexdigest()}
    def train(kind, payload, artifact, emit, cancelled, *, source_hashes):
        assert source_hashes == descriptor['source_hashes']
        emit({'training_sampling': {'start': '2024-05-06', 'end': '2024-05-10', 'episode_count': 1}})
        assert json.loads((control / 'training-sampling.json').read_text())['episode_count'] == 1
        emit({'training_sampling': sampling, 'training_sampling_intervals': [{'start': '2024-05-06', 'end': '2024-05-24'}]})
        if outcome == 'failed':
            raise ValueError('training failed')
        if outcome == 'cancelled':
            raise LearningCancelled('training cancelled')
        artifact.mkdir()
    monkeypatch.setattr(policy_jobs, 'execute_policy_job', train)
    descriptor = {'kind': 'train_policy', 'job_id': 'job', 'source_hashes': source_hashes,
                  'input': {'environment': {}, 'request': {}}}
    emitted = []
    if outcome == 'succeeded':
        jobs.execute_learning_job(descriptor, control, emitted.append, lambda: False)
    else:
        with pytest.raises(ValueError if outcome == 'failed' else BacktestCancelled):
            jobs.execute_learning_job(descriptor, control, emitted.append, lambda: False)
    assert json.loads((control / 'training-sampling.json').read_text()) == sampling
    assert emitted
