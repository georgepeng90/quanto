from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from quanto.api.app import create_app


@pytest.fixture
def history_workspace(tmp_path, monkeypatch):
    config = tmp_path / 'market.json'
    config.write_text(json.dumps({'root': str(tmp_path / 'market')}))
    monkeypatch.setenv('QUANTO_MARKET_CONFIG', str(config))
    app = create_app(tmp_path / 'workspace')
    with app.state.service.store.transaction() as db:
        db.execute('INSERT INTO investing_models VALUES (?,?,?,?)',
                   ('m1', 'dataset', json.dumps({'name': 'History model'}), '2026-01-01'))
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
    assert isinstance(summary['capabilities']['cuda_available'], bool)
    assert 'versions' not in summary and 'sessions' not in summary


def test_model_backtest_versions_include_saved_and_snapshot_rounds(history_workspace):
    store, client, headers = history_workspace
    add_sessions(store, ['succeeded'] * 245)
    add_sessions(store, ['failed', 'succeeded'], start=246, base='job-245')
    response = client.get('/api/investing/models/m1', headers=headers)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail['latest_version_id'] == 'job-247'
    assert detail['versions'][0]['round_number'] == 246
    assert detail['versions'][0]['best_round_number'] == 244
    assert detail['versions'][-1]['round_number'] == 1


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
