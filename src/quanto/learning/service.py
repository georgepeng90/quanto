"""Local learning workspace API; queued workers consume frozen trusted references."""
from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import sqlite3
from contextlib import closing
from uuid import uuid4

from quanto.learning.registry import hash_files, runtime_versions, verify_ref
from quanto.workbench.store import utc_now
from quanto.workbench.worker import atomic_json


RESOURCES = {'datasets': 'dataset', 'models': 'model', 'backtests': 'backtest'}


@lru_cache(maxsize=128)
def _verify_unchanged_file(path: str, expected: str, fingerprint: tuple) -> None:
    with Path(path).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise ValueError('INVALID_ARTIFACT: result file changed')


def verify_result_file(path: Path, expected: str) -> None:
    if path.is_symlink():
        raise ValueError('INVALID_ARTIFACT: symbolic result file')
    stat = path.stat()
    # Avoid rehashing a multi-GB index for every results page; mutation invalidates the cache.
    _verify_unchanged_file(str(path.resolve()), expected, (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))


def checkpoint_root(job) -> Path:
    # Cancellation/restart may win after the worker's atomic staging -> final move.
    base = job.artifact_root / job.id
    for phase in ('staging', 'final'):
        root = base / phase / 'artifact' / 'checkpoints'
        if (root / 'checkpoint.json').is_file():
            return root
    raise ValueError('no complete checkpoint is available for this training job')


@lru_cache(maxsize=1)
def capabilities() -> dict:
    value = {'python_version': platform.python_version(), 'torch_available': False,
             'cuda_available': False, 'device_name': 'CPU', 'torch_version': None}
    if importlib.util.find_spec('torch') is not None:
        try:
            import torch
            value.update(torch_available=True, torch_version=torch.__version__, cuda_available=torch.cuda.is_available())
            if value['cuda_available']:
                value.update(device_name=torch.cuda.get_device_name(0), cuda_version=torch.version.cuda,
                             gpu_memory_bytes=torch.cuda.get_device_properties(0).total_memory)
        except (ImportError, OSError, RuntimeError) as exc:
            value['error'] = str(exc)
    return value


class LearningService:
    resources = RESOURCES

    def __init__(self, workbench):
        self.workbench = workbench
        self.store = workbench.store

    def _row(self, identity: str, kind: str | None = None):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM learning_artifacts WHERE id=?', (identity,)).fetchone()
        if row is None or kind and row['kind'] != kind:
            raise KeyError(identity)
        return row

    @staticmethod
    def _public(row):
        return {'id': row['id'], 'job_id': row['id'], 'kind': row['kind'], 'state': 'succeeded',
                'created_at': row['created_at'], 'manifest': json.loads(row['manifest_json'])}

    def reference(self, identity, kind):
        row = self._row(identity, kind)
        ref = {'id': row['id'], 'path': row['path'], 'content_hash': row['content_hash'],
               'files': json.loads(row['files_json'])}
        verify_ref(ref)
        return ref

    def detail(self, resource, identity):
        if resource not in self.resources:
            raise KeyError(resource)
        row = self._row(identity, self.resources[resource])
        result = self._public(row)
        if resource in {'models', 'policies'}:
            path = Path(row['path']) / 'curves.json'
            if path.is_file():
                verify_result_file(path, json.loads(row['files_json'])['curves.json'])
                result['manifest']['history'] = json.loads(path.read_text(encoding='utf-8'))
        return result

    def listing(self, resource, cursor=None, limit=50):
        if resource not in self.resources or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid artifact resource or page size')
        offset = int(cursor or '0')
        if offset < 0:
            raise ValueError('invalid cursor')
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM learning_artifacts WHERE kind=? ORDER BY rowid DESC LIMIT ? OFFSET ?',
                              (self.resources[resource], limit + 1, offset)).fetchall()
        return {'items': [self._public(row) for row in rows[:limit]],
                'next_cursor': str(offset + limit) if len(rows) > limit else None}

    def snapshots(self):
        root = self.store.market_root
        catalog = root / 'catalog.sqlite'
        if not catalog.is_file():
            return []
        with closing(sqlite3.connect(f'{catalog.as_uri()}?mode=ro', uri=True)) as db:
            refs = db.execute('SELECT id,payload FROM snapshots ORDER BY rowid DESC LIMIT 200').fetchall()
        items = []
        for identity, payload in refs:
            path = root / 'snapshots' / identity / 'manifest.json'
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != identity:
                continue
            manifest = json.loads(raw)
            for request in manifest['requirement']['requests']:
                if request['kind'] != 'bars' or request.get('frequency') != '1m':
                    continue
                query = dict(request)
                if query['price_basis']['mode'] == 'vendor_research' and not query['price_basis'].get('basis_id'):
                    bases = {json.dumps(ref['request']['price_basis'], sort_keys=True) for ref in manifest['fragments']
                             if ref['request']['kind'] == 'bars' and ref['request'].get('frequency') == '1m'}
                    if len(bases) != 1:
                        continue
                    query['price_basis'] = json.loads(next(iter(bases)))
                items.append({'id': identity, 'manifest_digest': identity, 'frequency': '1m',
                              'instruments': request['instruments'], 'start': request['start'], 'end': request['end'],
                              'price_basis': query['price_basis'], 'limitations': manifest['coverage'].get('limitations', []), 'query': query})
                if len(items) == 50:
                    return items
        return items

    def overview(self):
        with self.store.transaction() as db:
            identities = [row[0] for row in db.execute("SELECT id FROM jobs WHERE kind IN ('prepare_learning','train','model_backtest') ORDER BY enqueue_seq DESC LIMIT 50")]
        jobs = []
        for identity in identities:
            record = self.store.get_job(identity)
            job = self.workbench.get_resource('jobs', identity)
            if record.input_path and Path(record.input_path).is_file():
                job['request'] = json.loads(Path(record.input_path).read_text(encoding='utf-8'))['input']['request']
            job['checkpoint_available'] = False
            if record.kind == 'train' and record.state in {'failed', 'cancelled', 'interrupted'}:
                try:
                    checkpoint_root(record)
                    job['checkpoint_available'] = True
                except ValueError:
                    pass
            jobs.append(job)
        return {'capabilities': {**capabilities(), 'disk_free_bytes': shutil.disk_usage(self.store.artifact_root).free},
                'snapshots': self.snapshots(), 'jobs': jobs,
                **{resource: self.listing(resource)['items'] for resource in RESOURCES}}

    def prepare(self, body: dict):
        if not isinstance(body, dict) or set(body) != {'spec'}:
            raise ValueError('prepare requires spec only')
        from quanto.learning.contracts import parse_prepare_spec
        from quanto.data.catalog import MarketCatalog
        from quanto.data.storage import MarketStore
        spec = parse_prepare_spec(body['spec'])
        snapshot = MarketCatalog(self.store.market_root).resolve_snapshot(spec['snapshot_id'])
        MarketStore(self.store.market_root).verify_snapshot(snapshot)
        return self._submit('prepare_learning', {'spec': spec}, {'market_root': str(self.store.market_root)})

    def train(self, body: dict):
        if not isinstance(body, dict) or not {'dataset_id', 'spec'} <= body.keys() or set(body) - {'dataset_id', 'spec', 'resume_job_id'}:
            raise ValueError('training requires dataset_id and spec')
        from quanto.learning.contracts import parse_training_spec
        spec = parse_training_spec(body['spec'])
        trusted = {'dataset_ref': self.reference(body['dataset_id'], 'dataset')}
        request = {**body, 'spec': spec}
        if body.get('resume_job_id'):
            job = self.store.get_job(body['resume_job_id'])
            if job.kind != 'train' or job.state not in {'failed', 'interrupted', 'cancelled'}:
                raise ValueError('resume requires a terminal interrupted/cancelled/failed training job')
            root = checkpoint_root(job)
            from quanto.learning.training import read_checkpoint
            read_checkpoint(root)
            trusted.update(resume_source=root)
        return self._submit('train', request, trusted)

    def backtest(self, body: dict):
        if not isinstance(body, dict) or not {'dataset_id', 'spec'} <= body.keys() or set(body) - {'dataset_id', 'model_id', 'spec', 'allow_in_sample'}:
            raise ValueError('backtest requires dataset_id and spec')
        from quanto.learning.intraday import IntradaySpec
        from quanto.learning.contracts import timestamp
        from quanto.learning.dataset import read_dataset_manifest
        from quanto.learning.artifacts import read_model_manifest
        spec = asdict(IntradaySpec.from_dict(body['spec']))
        if type(body.get('allow_in_sample', False)) is not bool:
            raise ValueError('allow_in_sample must be boolean')
        trusted = {'dataset_ref': self.reference(body['dataset_id'], 'dataset')}
        dataset = read_dataset_manifest(Path(trusted['dataset_ref']['path']))
        if timestamp(spec['start']) < timestamp(dataset['query']['start']) or timestamp(spec['end']) > timestamp(dataset['query']['end']):
            raise ValueError('backtest interval exceeds the prepared dataset query coverage')
        if spec['baseline'] == 'model':
            if not body.get('model_id'):
                raise ValueError('model_id is required for model backtests')
            trusted['model_ref'] = self.reference(body['model_id'], 'model')
            model = read_model_manifest(Path(trusted['model_ref']['path']))
            if model['frequency_minutes'] != dataset['frequency_minutes'] or model['feature_version'] != dataset['feature_version']:
                raise ValueError('model and dataset feature/frequency mismatch')
            if timestamp(spec['start']) < timestamp(model['cutoff']) and not body.get('allow_in_sample', False):
                raise ValueError('backtest starts before training/validation cutoff; explicitly allow_in_sample for research replay')
        request = {**body, 'spec': spec, 'allow_in_sample': body.get('allow_in_sample', False)}
        return self._submit('model_backtest', request, trusted)

    def _submit(self, kind, request, trusted, *, register=None):
        identity = str(uuid4())
        root = self.store.root / 'job-inputs' / identity
        root.mkdir(parents=True)
        source_root = Path(__file__).resolve().parents[3]
        try:
            shutil.copytree(source_root / 'src' / 'quanto', root / 'src' / 'quanto', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            trusted = dict(trusted)
            resume = trusted.pop('resume_source', None)
            if resume:
                # Copy only the immutable published checkpoint pair, not incomplete epochs.
                from quanto.learning.training import read_checkpoint
                read_checkpoint(resume)
                target = root / 'resume'
                target.mkdir()
                metadata = json.loads((resume / 'checkpoint.json').read_bytes())
                from quanto.objects import safe_name
                shutil.copy2(resume / safe_name(metadata['file']), target / safe_name(metadata['file']))
                shutil.copy2(resume / 'checkpoint.json', target / 'checkpoint.json')
                trusted.update(resume_dir=str(target), resume_files=hash_files(target))
            payload = {'request': request, 'environment': runtime_versions(), **trusted}
            hashes = hash_files(root)
            atomic_json(root / 'input.json', {'source_root': str(root), 'input': payload, 'source_hashes': hashes})
            with self.store.transaction() as db:
                seq = db.execute('SELECT COALESCE(MAX(enqueue_seq),0)+1 FROM jobs').fetchone()[0]
                db.execute("INSERT INTO jobs (id,kind,state,enqueue_seq,artifact_root,input_path,created_at) VALUES (?,?,'queued',?,?,?,?)",
                           (identity, kind, seq, str(self.store.artifact_root), str(root / 'input.json'), utc_now()))
                db.execute('INSERT INTO learning_submissions VALUES (?,?)',
                           (identity, hashlib.sha256((root / 'input.json').read_bytes()).hexdigest()))
                if register is not None:
                    register(db, identity)
        except BaseException:
            # This newly created directory is never shared or registered on failure.
            if not root.resolve().is_relative_to((self.store.root / 'job-inputs').resolve()):
                raise ValueError('invalid job input cleanup path')
            shutil.rmtree(root)
            raise
        return self.workbench.get_resource('jobs', identity)

    def result_root(self, identity):
        row = self._row(identity, self.resources['backtests'])
        root = Path(row['path'])
        # Queries verify the compact index; full exports verify every original row file.
        expected = json.loads(row['files_json'])['results.sqlite3']
        verify_result_file(root / 'results.sqlite3', expected)
        return root
