"""Daily ETF policy jobs using the existing immutable learning workspace."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil

from quanto.learning.service import LearningService, capabilities
from quanto.learning.registry import runtime_versions
from quanto.workbench.dataset_objects import ensure_dataset_object


class InvestingService(LearningService):
    resources = {'policies': 'policy', 'backtests': 'policy_backtest'}

    def overview(self):
        with self.store.transaction() as db:
            identities = [row[0] for row in db.execute("SELECT id FROM jobs WHERE kind IN ('train_policy','policy_backtest') ORDER BY enqueue_seq DESC LIMIT 50")]
            dataset_ids = [row[0] for row in db.execute('SELECT id FROM datasets ORDER BY rowid DESC LIMIT 100')]
        jobs = []
        for identity in identities:
            record = self.store.get_job(identity)
            job = self.workbench.get_resource('jobs', identity)
            if record.input_path and Path(record.input_path).is_file():
                job['request'] = json.loads(Path(record.input_path).read_text(encoding='utf-8'))['input']['request']
            jobs.append(job)
        return {'capabilities': {**capabilities(), 'algorithm': 'PPO', 'frequencies': ['weekly', 'monthly'],
                                 'ppo_available': importlib.util.find_spec('stable_baselines3') is not None,
                                 'runtime': runtime_versions(),
                                 'disk_free_bytes': shutil.disk_usage(self.store.artifact_root).free},
                'datasets': [self.workbench.get_resource('datasets', identity) for identity in dataset_ids],
                'jobs': jobs, **{name: self.listing(name)['items'] for name in self.resources}}

    def _dataset(self, identity, spec):
        from quanto.backtest.dataset_loaders import load_dataset_for_run
        version = self.store.get_dataset(identity)
        root, files = ensure_dataset_object(self.store.root, version)
        spec.validate_for_dataset(load_dataset_for_run(root, spec))
        return {'id': version.id, 'path': str(root), 'content_hash': version.content_hash, 'files': files}

    def train(self, body):
        if not isinstance(body, dict) or set(body) != {'dataset_id', 'spec'}:
            raise ValueError('training requires dataset_id and spec only')
        if not isinstance(body['dataset_id'], str) or not body['dataset_id'].strip():
            raise ValueError('dataset_id must be a nonempty string')
        from quanto.investing.contracts import PolicyTrainingSpec
        spec = PolicyTrainingSpec.model_validate(body['spec'])
        ref = self._dataset(body['dataset_id'], spec.backtest)
        return self._submit('train_policy', {'dataset_id': body['dataset_id'], 'spec': spec.model_dump(mode='json')}, {'dataset_ref': ref})

    def backtest(self, body):
        if not isinstance(body, dict) or set(body) != {'dataset_id', 'policy_id', 'spec'}:
            raise ValueError('backtest requires dataset_id, policy_id and spec only')
        if any(not isinstance(body[key], str) or not body[key].strip() for key in ('dataset_id', 'policy_id')):
            raise ValueError('dataset_id and policy_id must be nonempty strings')
        from quanto.investing.contracts import PolicyBacktestSpec, PolicyTrainingSpec
        from quanto.learning.artifacts import read_manifest
        spec = PolicyBacktestSpec.model_validate(body['spec'])
        policy = self.reference(body['policy_id'], 'policy')
        manifest = read_manifest(Path(policy['path']), 'policy')
        training = PolicyTrainingSpec.model_validate(manifest['config'])
        if spec.start <= training.validation_end:
            raise ValueError('backtest must start after validation_end; training and selection periods are excluded')
        daily = training.backtest.model_copy(update={'start': spec.start, 'end': spec.end})
        dataset = self._dataset(body['dataset_id'], daily)
        return self._submit('policy_backtest', {**body, 'spec': spec.model_dump(mode='json')},
                            {'dataset_ref': dataset, 'policy_ref': policy})
