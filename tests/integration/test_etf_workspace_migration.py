import json
from hashlib import sha256

import pytest

from quanto.objects import digest, json_bytes
from quanto.learning.registry import hash_files
from quanto.workbench.bindings import BindingStore
from quanto.workbench.framework_requests import FixedRequest
from quanto.workbench.etf_workspace_migration import migrate_etf_workspace
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
