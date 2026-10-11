"""Offline, atomic conversion of an explicitly declared ETF workspace.

All format conversion lives here. Running services consume current records only;
original prices, executable snapshots, weights and completed results stay intact.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
from contextlib import closing
from types import SimpleNamespace

from quanto.backtest.dataset_identity import standard_table_semantics, validate_binding_tables
from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.trading import ETF_TRADING_DEFINITION
from quanto.data.contracts import InstrumentId
from quanto.framework import AssetBinding, AssetMapping, BenchmarkBinding, canonical_hash
from quanto.objects import digest, json_bytes
from quanto.workbench.baskets import BasketStore
from quanto.workbench.bindings import BindingStore
from quanto.workbench.dataset_objects import verify_dataset_version
from quanto.workbench.framework_requests import FixedRequest
from quanto.workbench.scheduler import Scheduler
from quanto.workbench.store import WorkbenchStore, utc_now


def _rows(store, sql, parameters=()):
    with store.transaction() as db:
        return [dict(row) for row in db.execute(sql, parameters)]


def _identities(values):
    result = {symbol: InstrumentId.model_validate(value) for symbol, value in values.items()}
    if any(value.instrument_type != 'etf' for value in result.values()):
        raise ValueError('This migration requires an explicit ETF declaration for every asset')
    return result


def _bind_datasets(store, identities, source_document):
    bindings = BindingStore(store)
    evidence = []
    for row in _rows(store, 'SELECT id FROM datasets ORDER BY id'):
        dataset = store.get_dataset(row['id'])
        path = verify_dataset_version(dataset)
        bundle = load_dataset(path)
        symbols = list(bundle.instruments.symbol)
        if set(symbols) - identities.keys():
            raise ValueError(f'Missing ETF identity declaration: {sorted(set(symbols) - identities.keys())}')
        declaration = {'source_document': source_document, 'dataset_id': dataset.id,
                       'dataset_content_hash': dataset.content_hash,
                       'assets': {symbol: identities[symbol].model_dump(mode='json') for symbol in symbols}}
        ref = 'etf-migration:' + canonical_hash(declaration)
        evidence.append(declaration)
        try:
            existing = bindings.get_asset_binding(dataset.id)
        except KeyError:
            existing = None
        if existing is not None:
            if {a.local_symbol: a.instrument_id for a in existing.assets} != {s: identities[s] for s in symbols}:
                raise ValueError('Existing dataset identity differs from ETF declaration')
            continue
        benchmark_file = next(path / f'benchmark.{suffix}' for suffix in ('parquet', 'csv')
                              if (path / f'benchmark.{suffix}').is_file())
        binding = AssetBinding(dataset_version_id=dataset.id, dataset_content_hash=dataset.content_hash,
            assets=tuple(AssetMapping(local_symbol=s, instrument_id=identities[s], evidence_ref=ref) for s in symbols),
            benchmark=BenchmarkBinding(custom_series_id='benchmark-' + digest(benchmark_file.read_bytes()),
                                       return_basis=bundle.manifest.benchmark_type, evidence_ref=ref),
            table_semantics=standard_table_semantics(bundle, ref))
        validate_binding_tables(bundle, binding.assets, binding.table_semantics)
        with store.transaction() as db:
            bindings._insert_asset_binding(db, binding)
    return evidence


def _basket(store, dataset_id, symbols):
    binding = BindingStore(store).get_asset_binding(dataset_id)
    by_local = {a.local_symbol: a.instrument_id for a in binding.assets}
    try:
        members = tuple(by_local[symbol] for symbol in symbols)
    except KeyError as exc:
        raise ValueError('Saved configuration has a symbol without ETF identity') from exc
    key = canonical_hash([member.model_dump(mode='json') for member in members])
    return BasketStore(store).create(f'ETF组合（{len(members)}只）', members,
        '保留原配置的标的顺序', f'etf-unification:{key}')


def _convert_experiments(store):
    from quanto.workbench.configuration import FrameworkResolver
    for row in _rows(store, 'SELECT * FROM experiments ORDER BY id'):
        payload = json.loads(row['payload_json'])
        if 'framework_contract_version' not in payload:
            from quanto.config import validate_parameters
            strategy = store.get_strategy(payload['strategy_revision_id'])
            effective = validate_parameters(strategy.manifest['parameter_schema'], payload['strategy_params'])
            if 'effective_strategy_params' in payload and payload['effective_strategy_params'] != effective:
                raise ValueError('Saved effective strategy parameters differ from current request')
            dataset_id = payload['dataset_version_id']
            spec = json.loads(json.dumps(payload['backtest_spec']))
            symbols = spec.pop('symbols')
            basket = _basket(store, dataset_id, symbols)
            binding = BindingStore(store).get_asset_binding(dataset_id)
            canonical = {a.local_symbol: a.instrument_id.key for a in binding.assets}
            for scenario in spec['risk'].get('stress_scenarios', []):
                scenario['symbol_shocks'] = {canonical[key]: value for key, value in scenario['symbol_shocks'].items()}
            payload = {'framework_contract_version': 'multi-asset-v1',
                       'basket_revision_ref': basket.ref.model_dump(mode='json'),
                       'dataset_version_id': dataset_id, 'strategy_revision_id': payload['strategy_revision_id'],
                       'trading_definition_ref': ETF_TRADING_DEFINITION.ref.model_dump(mode='json'),
                       'strategy_params': payload['strategy_params'], 'backtest_spec': spec,
                       'sample_purpose': payload['sample_purpose']}
        request = FixedRequest.model_validate(payload)
        # Drafts may have genuine data/warmup restrictions; preserve those rather
        # than changing their economic configuration to manufacture readiness.
        FrameworkResolver(store).check(request)
        normalized = request.model_dump_json()
        with store.transaction() as db:
            db.execute('UPDATE experiments SET payload_json=? WHERE id=?', (normalized, row['id']))


def _adopt_policies(store):
    """Include standalone training outputs in normal model/version management."""
    from quanto.learning.registry import verify_ref
    from quanto.learning.artifacts import read_manifest
    orphans = _rows(store, '''SELECT a.*,j.input_path FROM learning_artifacts a
        JOIN jobs j ON j.id=a.id LEFT JOIN investing_sessions s ON s.job_id=a.id
        WHERE a.kind='policy' AND s.job_id IS NULL ORDER BY j.enqueue_seq''')
    for row in orphans:
        files = json.loads(row['files_json'])
        manifest = read_manifest(verify_ref({'path': row['path'], 'files': files}), 'policy')
        if manifest != json.loads(row['manifest_json']) or digest(json_bytes(files)) != row['content_hash']:
            raise ValueError('Standalone policy metadata differs from its immutable artifact')
        raw = Path(row['input_path']).read_bytes()
        submission = _rows(store, 'SELECT input_hash FROM learning_submissions WHERE job_id=?', (row['id'],))
        if len(submission) != 1 or digest(raw) != submission[0]['input_hash']:
            raise ValueError('Standalone policy frozen input differs')
        request = json.loads(raw)['input']['request']
        model_id = 'model-' + row['id']
        with store.transaction() as db:
            db.execute('INSERT INTO investing_models VALUES (?,?,?,?)',
                       (model_id, request['dataset_id'], json.dumps(manifest['config'], ensure_ascii=False), row['created_at']))
            db.execute('INSERT INTO investing_sessions VALUES (?,?,?)', (row['id'], model_id, None))
    return len(orphans)


def _verify_frozen_files(path, document, key):
    from quanto.learning.registry import hash_files
    actual = hash_files(path.parent)
    actual.pop(path.name, None)
    if actual != document[key]:
        raise ValueError('Frozen execution source file hash differs')


def _historical_fixed_context(manifest, *, input_hash, asset_binding, basket_revision_ref):
    """Interpret original schema-2 evidence only while converting the workspace."""
    from quanto.workbench.run_contexts import create_context

    if manifest.get('schema_version') != 2 or 'resolved_configuration' in manifest:
        raise ValueError('Expected an original schema-2 fixed manifest')
    strategy = manifest['strategy_manifest']
    name = strategy.get('name') or strategy['entrypoint']
    definitions = {key: manifest[key] for key in
                   ('strategy_content_hash', 'execution_definition_version',
                    'risk_definition_version', 'metric_definition_version')}
    return create_context(kind='fixed', dataset_id=manifest['dataset_version_id'],
        asset_binding=asset_binding, ordered_symbols=manifest['backtest_spec']['symbols'],
        backtest_spec=manifest['backtest_spec'], basket_revision_ref=basket_revision_ref,
        execution_provenance={'source_hashes': manifest['files'],
                              'environment_fingerprint': canonical_hash(manifest['environment'])},
        input_hash=input_hash, name=str(name), method=str(name),
        dataset_source=manifest['dataset_manifest'].get('source'),
        sample_purpose=manifest.get('sample_purpose'), definition_evidence=definitions,
        strategy_revision_id=manifest['strategy_revision_id'])


def _historical_policy_context(document, policy_manifest, *, input_hash, asset_binding,
                               basket_revision_ref, dataset_source=None):
    """Interpret a frozen policy backtest only while converting the workspace."""
    from quanto.workbench.run_contexts import create_context

    payload = document['input']
    request = payload['request']
    if payload['dataset_ref']['content_hash'] != asset_binding.dataset_content_hash:
        raise ValueError('Policy backtest dataset hash differs from binding')
    if request['policy_id'] != payload['policy_ref']['id']:
        raise ValueError('Policy backtest version differs from frozen reference')
    training = policy_manifest['config']
    spec = json.loads(json.dumps(training['backtest']))
    backtest = request['spec']
    spec['start'], spec['end'] = backtest['start'], backtest['end']
    spec['execution'] = dict(spec['execution'])
    for key in ('commission_rate', 'minimum_commission', 'slippage_rate'):
        spec['execution'][key] *= backtest.get('cost_multiplier', 1)
    baseline = backtest.get('baseline', 'policy')
    name = str(training.get('name') or 'PPO') if baseline == 'policy' else baseline
    definitions = {key: policy_manifest[key] for key in
                   ('action_version', 'reward_version', 'feature_version', 'execution_version',
                    'environment_version') if key in policy_manifest}
    definitions['policy_content_id'] = policy_manifest['id']
    environment = payload.get('execution_environment', payload.get('environment'))
    if not isinstance(environment, dict):
        raise ValueError('Policy backtest frozen environment missing')
    return create_context(kind='policy', dataset_id=request['dataset_id'],
        asset_binding=asset_binding, ordered_symbols=spec['symbols'], backtest_spec=spec,
        execution_provenance={'source_hashes': document['source_hashes'],
                              'environment_fingerprint': canonical_hash(environment)},
        input_hash=input_hash, basket_revision_ref=basket_revision_ref,
        name=name, method='PPO' if baseline == 'policy' else baseline,
        dataset_source=dataset_source, evaluation_purpose=backtest.get('evaluation_purpose'),
        definition_evidence=definitions, model_id=policy_manifest.get('model_id'),
        policy_version_id=request['policy_id'], baseline=baseline)


def _convert_contexts(store, workspace_root):
    from quanto.workbench.run_contexts import ContextStore, fixed_context_from_manifest
    from quanto.learning.registry import verify_ref
    from quanto.learning.artifacts import read_manifest
    contexts = ContextStore(store)
    for run in _rows(store, 'SELECT * FROM runs ORDER BY rowid'):
        if _rows(store, 'SELECT 1 FROM run_contexts WHERE job_id=?', (run['job_id'],)):
            contexts.get(run['job_id'])
            continue
        raw = Path(run['manifest_path']).read_bytes()
        if digest(raw) != run['manifest_hash']:
            raise ValueError('Frozen fixed input hash differs')
        manifest = json.loads(raw)
        _verify_frozen_files(Path(run['manifest_path']), manifest, 'files')
        if 'dataset_ref' in manifest:
            from quanto.workbench.dataset_objects import verify_dataset_object
            ref = manifest['dataset_ref']
            verify_dataset_object(workspace_root, {'version_id': manifest['dataset_version_id'],
                                  'content_hash': ref['content_hash'], 'files': ref['files']})
        dataset_id = manifest['dataset_version_id']
        binding = BindingStore(store).get_asset_binding(dataset_id)
        if manifest['dataset_content_hash'] != binding.dataset_content_hash:
            raise ValueError('Frozen fixed dataset differs from registered identity')
        basket = _basket(store, dataset_id, manifest['backtest_spec']['symbols'])
        builder = (fixed_context_from_manifest if 'resolved_configuration' in manifest
                   else _historical_fixed_context)
        context = builder(manifest, input_hash=run['manifest_hash'],
                          asset_binding=binding, basket_revision_ref=basket.ref)
        with store.transaction() as db:
            contexts.insert(db, run['job_id'], context)
    for job in _rows(store, "SELECT * FROM jobs WHERE kind='policy_backtest' ORDER BY enqueue_seq"):
        if _rows(store, 'SELECT 1 FROM run_contexts WHERE job_id=?', (job['id'],)):
            contexts.get(job['id'])
            continue
        raw = Path(job['input_path']).read_bytes()
        saved = _rows(store, 'SELECT input_hash FROM learning_submissions WHERE job_id=?', (job['id'],))
        if len(saved) != 1 or digest(raw) != saved[0]['input_hash']:
            raise ValueError('Frozen policy backtest input hash differs')
        document = json.loads(raw)
        _verify_frozen_files(Path(job['input_path']), document, 'source_hashes')
        payload = document['input']
        for name in ('dataset_ref', 'training_dataset_ref', 'policy_ref'):
            if name in payload:
                ref = payload[name]
                if digest(json_bytes(ref['files'])) != ref['content_hash']:
                    raise ValueError('Frozen policy input reference hash differs')
                verify_ref(ref)
        dataset_id = payload['request']['dataset_id']
        binding = BindingStore(store).get_asset_binding(dataset_id)
        policies = _rows(store, "SELECT * FROM learning_artifacts WHERE id=? AND kind='policy'",
                         (payload['request']['policy_id'],))
        if len(policies) != 1:
            raise ValueError('Backtest policy is absent')
        policy = policies[0]
        if policy['content_hash'] != payload['policy_ref']['content_hash']:
            raise ValueError('Frozen policy content differs from registered artifact')
        manifest = read_manifest(verify_ref({'path': policy['path'], 'files': json.loads(policy['files_json'])}), 'policy')
        basket = _basket(store, dataset_id, manifest['config']['backtest']['symbols'])
        context = _historical_policy_context(document, manifest, input_hash=saved[0]['input_hash'],
            asset_binding=binding, basket_revision_ref=basket.ref,
            dataset_source=store.get_dataset(dataset_id).manifest.get('source'))
        if job['state'] == 'succeeded':
            results = _rows(store, "SELECT manifest_json FROM learning_artifacts WHERE id=? AND kind='policy_backtest'",
                            (job['id'],))
            if len(results) != 1:
                raise ValueError('Completed policy backtest result is absent')
            summary = json.loads(results[0]['manifest_json'])
            if (summary['period'] != context['conditions']['period']
                    or summary['baseline'] != context['baseline']
                    or summary['assumptions']['execution'] != context['backtest_spec']['execution']
                    or summary['assumptions']['price_mode'] != context['conditions']['price_mode']
                    or summary['policy_content_id'] != manifest['id']):
                raise ValueError('Policy backtest context differs from saved economic conditions')
        sessions = _rows(store, 'SELECT model_id FROM investing_sessions WHERE job_id=?', (policy['id'],))
        if len(sessions) != 1:
            raise ValueError('Policy must belong to exactly one current model')
        context['model_id'] = sessions[0]['model_id']
        with store.transaction() as db:
            contexts.insert(db, job['id'], context)


def _verify(store):
    from quanto.workbench.backtest_catalog import catalog
    from quanto.workbench.service import WorkbenchService
    service = WorkbenchService(store.root)
    page, count = None, 0
    while True:
        page = catalog(service, page['next_cursor'] if page else None, 100)
        for row in page['items']:
            if row['asset_types'] != ['etf'] or row.get('result_error'):
                raise ValueError(f"Backtest validation failed: {row['id']} {row.get('result_error')}")
            count += 1
        if not page['next_cursor']:
            break
    with store.transaction() as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Migrated database failed integrity checks')
        expected = db.execute("SELECT count(*) FROM jobs WHERE kind IN ('backtest','policy_backtest')").fetchone()[0]
        if count != expected:
            raise ValueError('Not every backtest has a current context')
        return {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in
                ('datasets', 'experiments', 'investing_models', 'learning_artifacts', 'run_contexts', 'policy_version_contexts')}


def migrate_etf_workspace(root: Path, identities: dict, *, source_document: str, apply: bool = False) -> dict:
    """Validate a database copy completely, then optionally replace it atomically.

    Caller stops the service and retains a full workspace/market backup. The
    scheduler lock excludes normal writers; the original DB is never migrated in
    place. Failed/preview stages are retained under backups for inspection.
    """
    root = Path(root).resolve()
    if not source_document.strip():
        raise ValueError('ETF identity source_document is required')
    identities = _identities(identities)
    source = root / 'workbench.sqlite3'
    if not source.is_file():
        raise ValueError('Existing workspace database is required')
    lock = Scheduler(SimpleNamespace(root=root))
    try:
        lock._acquire()
    except RuntimeError as exc:
        raise RuntimeError('Stop the running workspace service before migration') from exc
    try:
        original = source.read_bytes()
        backups = root / 'backups'
        backups.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='etf-unification-', dir=backups))
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(stage / source.name)) as dst:
            if src.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise ValueError('Offline migration requires a checkpointed DELETE-journal database')
            if src.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','cancel_requested')").fetchone()[0]:
                raise ValueError('Active jobs prevent offline migration')
            src.backup(dst)
        config = json.loads((root / 'workspace.json').read_text(encoding='utf-8'))
        artifact_root = Path(config['artifact_root'])
        if not artifact_root.is_absolute():
            artifact_root = root / artifact_root
        store = WorkbenchStore(stage, artifact_root=artifact_root)
        already_current = all(_rows(store, f'SELECT count(*) n FROM {a}')[0]['n'] ==
            _rows(store, f'SELECT count(*) n FROM {b}')[0]['n'] for a, b in
            [('datasets', 'dataset_asset_bindings'), ('investing_models', 'model_framework_bindings')])
        already_current = already_current and not _rows(store, "SELECT 1 FROM experiments WHERE json_extract(payload_json,'$.framework_contract_version') IS NULL")
        already_current = already_current and _rows(store, 'SELECT count(*) n FROM run_contexts')[0]['n'] == _rows(store, "SELECT count(*) n FROM jobs WHERE kind IN ('backtest','policy_backtest')")[0]['n']
        already_current = already_current and _rows(store, 'SELECT count(*) n FROM policy_version_contexts')[0]['n'] == _rows(store, "SELECT count(*) n FROM learning_artifacts WHERE kind='policy'")[0]['n']
        evidence = _bind_datasets(store, identities, source_document)
        _convert_experiments(store)
        adopted = _adopt_policies(store)
        from quanto.workbench.etf_model_migration import ModelMigrator
        for model in _rows(store, 'SELECT id FROM investing_models ORDER BY id'):
            ModelMigrator(store).migrate_model(model['id'])
        _convert_contexts(store, root)
        counts = _verify(store)
        report = {**counts, 'adopted_models': adopted, 'already_current': already_current,
                  'applied': apply and not already_current, 'source_document': source_document, 'dataset_evidence': evidence,
                  'source_database_sha256': digest(original), 'created_at': utc_now()}
        if source.read_bytes() != original:
            raise ValueError('Original workspace changed during migration')
        (stage / 'migration-report.json').write_bytes(json_bytes(report))
        if apply and not already_current:
            (stage / 'original.sqlite3').write_bytes(original)
            os.replace(stage / source.name, source)
        return report
    finally:
        lock._release()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--identities', required=True, type=Path,
                        help='JSON with source_document and identities mapping local symbols to InstrumentId')
    parser.add_argument('--apply', action='store_true', help='Publish only after complete staged validation')
    args = parser.parse_args()
    declaration = json.loads(args.identities.read_text(encoding='utf-8'))
    result = migrate_etf_workspace(args.workspace, declaration['identities'],
                                  source_document=declaration['source_document'], apply=args.apply)
    print(json.dumps({key: value for key, value in result.items() if key != 'dataset_evidence'}, ensure_ascii=False))
