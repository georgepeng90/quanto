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

    def detail(self, resource, identity):
        artifact = super().detail(resource, identity)
        if resource == 'backtests':
            from quanto.workbench.run_contexts import ContextStore
            artifact['context'] = ContextStore(self.store).get(identity)
        return artifact

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
        raise ValueError('Create a model and submit training through /investing/models/{model_id}/train')

    def backtest(self, body):
        raise ValueError('Submit saved-version backtests through /investing/models/{model_id}/backtests')
