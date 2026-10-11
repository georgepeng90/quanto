import json
from hashlib import sha256
from pathlib import Path

import pytest

from quanto.objects import digest, json_bytes
from quanto.learning.registry import hash_files
from quanto.workbench.bindings import BindingStore
from quanto.workbench.framework_requests import FixedRequest
from quanto.workbench.etf_workspace_migration import migrate_etf_workspace
from quanto.workbench.revisions import source_files
from quanto.workbench.runs import RunService
from tests.integration.test_snapshot_submission import _ready


def old_workspace(tmp_path):
    store, draft, dataset, _ = _ready(tmp_path)
    source = tmp_path / 'source'
    payload = dict(draft.payload)
    for key in ('framework_contract_version', 'basket_revision_ref', 'trading_definition_ref',
                'source_run_id', 'source_experiment_ref'):
        payload.pop(key, None)
    payload['backtest_spec'] = {**payload['backtest_spec'], 'symbols': ['000001']}
    with store.transaction() as db:
        db.execute('DELETE FROM dataset_asset_bindings')
        db.execute('DELETE FROM framework_idempotency')
        db.execute('DELETE FROM basket_revisions')
        db.execute('DELETE FROM baskets')
        db.execute('UPDATE datasets SET path=?,content_hash=? WHERE id=?',
                   (str(source), digest(json_bytes(hash_files(source))), dataset.id))
        db.execute('UPDATE experiments SET payload_json=? WHERE id=?', (json.dumps(payload), draft.id))
    return store, draft, source


IDENTITIES = {'000001': {'exchange': 'SZSE', 'instrument_type': 'etf', 'code': '000001'}}


def _old_strategy(store, strategy):
    manifest = strategy.manifest
    old = {key: manifest[key] for key in ('auxiliary_sources', 'entrypoint', 'name',
            'parameter_schema', 'required_data_columns', 'research_data')}
    (strategy.path / 'strategy.json').write_bytes(json_bytes(old))
    files = source_files(strategy.path)
    content_hash = digest(json_bytes({name: digest(value) for name, value in sorted(files.items())}))
    with store.transaction() as db:
        db.execute('UPDATE strategies SET content_hash=?,manifest_json=? WHERE id=?',
                   (content_hash, json_bytes(old).decode(), strategy.id))
    return files, content_hash


def test_migration_converts_all_drafts_without_rewriting_prices(tmp_path):
    store, draft, source = old_workspace(tmp_path)
    before = hash_files(source)
    report = migrate_etf_workspace(store.root, IDENTITIES, source_document='Explicit test ETF identities', apply=True)
    assert report['datasets'] == 1 and report['experiments'] == 1
    converted = store.get_experiment(draft.id)
    request = FixedRequest.model_validate(converted.payload)
    assert converted.revision == draft.revision
    assert 'symbols' not in request.backtest_spec.model_dump()
    binding = BindingStore(store).get_asset_binding(request.dataset_version_id)
    assert binding.assets[0].local_symbol == '000001'
    assert binding.assets[0].instrument_id.key == 'SZSE:etf:000001'
    assert hash_files(source) == before
    snapshot = store.db_path.read_bytes()
    repeated = migrate_etf_workspace(store.root, IDENTITIES, source_document='Explicit test ETF identities', apply=True)
    assert repeated['already_current'] is True
    assert repeated['applied'] is False
    assert store.db_path.read_bytes() == snapshot


def test_preview_leaves_database_and_data_unchanged(tmp_path):
    store, _, source = old_workspace(tmp_path)
    before = sha256(store.db_path.read_bytes()).hexdigest(), hash_files(source)
    report = migrate_etf_workspace(store.root, IDENTITIES, source_document='Explicit test ETF identities')
    assert report['applied'] is False and report['datasets'] == 1
    assert (sha256(store.db_path.read_bytes()).hexdigest(), hash_files(source)) == before


def test_missing_identity_aborts_without_partial_database_update(tmp_path):
    store, _, source = old_workspace(tmp_path)
    before = store.db_path.read_bytes(), hash_files(source)
    with pytest.raises(ValueError, match='identity'):
        migrate_etf_workspace(store.root, {}, source_document='Missing mapping', apply=True)
    assert (store.db_path.read_bytes(), hash_files(source)) == before


def test_migration_rejects_changed_effective_strategy_parameters(tmp_path):
    store, draft, _ = old_workspace(tmp_path)
    payload = store.get_experiment(draft.id).payload
    payload['effective_strategy_params'] = {'unexpected_parameter': 42}
    with store.transaction() as db:
        db.execute('UPDATE experiments SET payload_json=? WHERE id=?', (json.dumps(payload), draft.id))
    before = store.db_path.read_bytes()
    with pytest.raises(ValueError, match='effective strategy parameters'):
        migrate_etf_workspace(store.root, IDENTITIES, source_document='Declared ETF', apply=True)
    assert store.db_path.read_bytes() == before


def test_non_etf_declaration_is_rejected(tmp_path):
    store, _, _ = old_workspace(tmp_path)
    mapping = {'000001': {**IDENTITIES['000001'], 'instrument_type': 'stock'}}
    with pytest.raises(ValueError, match='ETF'):
        migrate_etf_workspace(store.root, mapping, source_document='Wrong type', apply=True)


def test_running_scheduler_prevents_migration(tmp_path):
    from quanto.workbench.scheduler import Scheduler
    store, _, _ = old_workspace(tmp_path)
    scheduler = Scheduler(store)
    scheduler._acquire()
    try:
        with pytest.raises(RuntimeError, match='running'):
            migrate_etf_workspace(store.root, IDENTITIES, source_document='Declared ETF', apply=True)
    finally:
        scheduler._release()


def test_frozen_source_tampering_aborts_migration(tmp_path):
    from quanto.workbench.runs import RunService
    store, draft, dataset, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, 'migration-check', 'web')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='failed' WHERE id=?", (run.job_id,))
        db.execute('DELETE FROM run_contexts WHERE job_id=?', (run.job_id,))
    (run.manifest_path.parent / 'strategy/strategy.py').write_text('changed', encoding='utf-8')
    before = store.db_path.read_bytes()
    mapping = {'SZSE:etf:000001': IDENTITIES['000001']}
    with pytest.raises(ValueError, match='Frozen execution source'):
        migrate_etf_workspace(store.root, mapping, source_document='Declared ETF', apply=True)
    assert store.db_path.read_bytes() == before


def test_old_strategy_declaration_is_replaced_without_changing_code_or_frozen_run(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from quanto.api.app import create_app

    store, draft, _, strategy = _ready(tmp_path)
    strategy_code = (Path(__file__).resolve().parents[2] / 'strategies' / 'buy_hold' / 'strategy.py').read_bytes()
    (strategy.path / 'strategy.py').write_bytes(strategy_code)
    current_files = source_files(strategy.path)
    with store.transaction() as db:
        db.execute('UPDATE strategies SET content_hash=? WHERE id=?',
                   (digest(json_bytes({name: digest(data) for name, data in sorted(current_files.items())})), strategy.id))
    original = RunService(store).submit(draft.id, draft.revision, 'original', 'web')
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='failed' WHERE id=?", (original.job_id,))
    frozen = original.manifest_path.read_bytes()
    old_files, old_hash = _old_strategy(store, strategy)
    before = store.db_path.read_bytes()
    mapping = {'SZSE:etf:000001': IDENTITIES['000001']}
    preview = migrate_etf_workspace(store.root, mapping, source_document='Declared ETF')
    assert preview['converted_strategies'] == 1 and store.db_path.read_bytes() == before
    assert not (store.root / 'strategies' / 'etf-current').exists()
    report = migrate_etf_workspace(store.root, mapping, source_document='Declared ETF', apply=True)
    assert report['applied'] is True and report['converted_strategies'] == 1
    assert report['strategy_evidence'][0]['original_content_hash'] == old_hash
    converted = store.get_strategy(strategy.id)
    assert converted.path != strategy.path and converted.path.is_relative_to(store.root)
    assert converted.manifest['parameter_schema'] == json.loads(old_files['strategy.json'])['parameter_schema']
    assert {name: value for name, value in source_files(converted.path).items() if name != 'strategy.json'} == {
        name: value for name, value in old_files.items() if name != 'strategy.json'}
    assert source_files(strategy.path) == old_files
    assert original.manifest_path.read_bytes() == frozen
    repeated = RunService(store).rerun(original.run_id, 'current-from-old', 'web')
    assert repeated.parent_run_id == original.run_id
    assert json.loads(repeated.manifest_path.read_bytes())['strategy_content_hash'] == converted.content_hash
    app = create_app(store.root)
    with TestClient(app, base_url='http://127.0.0.1'):
        deadline = time.monotonic() + 30
        while store.get_job(repeated.job_id).state in {'queued', 'running'} and time.monotonic() < deadline:
            time.sleep(.05)
    assert store.get_job(repeated.job_id).state == 'succeeded'
    snapshot = store.db_path.read_bytes()
    second = migrate_etf_workspace(store.root, mapping, source_document='Declared ETF', apply=True)
    assert second['already_current'] is True and second['converted_strategies'] == 0
    assert store.db_path.read_bytes() == snapshot


def test_invalid_old_strategy_declaration_aborts_without_publication(tmp_path):
    store, _, _, strategy = _ready(tmp_path)
    _old_strategy(store, strategy)
    original = source_files(strategy.path)
    manifest = json.loads(original['strategy.json'])
    manifest['required_data_columns'].append('undocumented_field')
    (strategy.path / 'strategy.json').write_bytes(json_bytes(manifest))
    files = source_files(strategy.path)
    with store.transaction() as db:
        db.execute('UPDATE strategies SET content_hash=?,manifest_json=? WHERE id=?',
                   (digest(json_bytes({name: digest(data) for name, data in sorted(files.items())})),
                    json_bytes(manifest).decode(), strategy.id))
    before = store.db_path.read_bytes()
    with pytest.raises(ValueError, match='required price field'):
        migrate_etf_workspace(store.root, {'SZSE:etf:000001': IDENTITIES['000001']},
                              source_document='Declared ETF', apply=True)
    assert store.db_path.read_bytes() == before and source_files(strategy.path) == files
    assert not (store.root / 'strategies' / 'etf-current').exists()


def test_failed_database_replacement_preserves_original_and_can_retry(tmp_path, monkeypatch):
    import quanto.workbench.etf_workspace_migration as migration

    store, _, _, strategy = _ready(tmp_path)
    old_files, _ = _old_strategy(store, strategy)
    before = store.db_path.read_bytes()
    mapping = {'SZSE:etf:000001': IDENTITIES['000001']}
    with monkeypatch.context() as patch:
        patch.setattr(migration.os, 'replace', lambda *_: (_ for _ in ()).throw(OSError('replace failed')))
        with pytest.raises(OSError, match='replace failed'):
            migrate_etf_workspace(store.root, mapping, source_document='Declared ETF', apply=True)
    assert store.db_path.read_bytes() == before and source_files(strategy.path) == old_files
    reports = list((store.root / 'backups').glob('etf-unification-*/migration-report.json'))
    assert len(reports) == 1 and json.loads(reports[0].read_bytes())['applied'] is False
    retried = migrate_etf_workspace(store.root, mapping, source_document='Declared ETF', apply=True)
    assert retried['applied'] is True and retried['converted_strategies'] == 1
