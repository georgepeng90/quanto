"""Persistent fixed-basket model identities and serialized training sessions."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from quanto.investing.contracts import PolicyBacktestSpec, PolicyTrainingSpec
from quanto.investing.service import InvestingService
from quanto.learning.artifacts import read_manifest
from quanto.objects import json_bytes
from quanto.workbench.store import utc_now


ACTIVE_STATES = {'queued', 'running', 'cancel_requested'}


def _identity(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a nonempty string')
    return value


class InvestingModels(InvestingService):
    def _model(self, identity):
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM investing_models WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return dict(row)

    def _sessions(self, identity):
        with self.store.transaction() as db:
            return db.execute('''SELECT s.job_id, s.base_version_id, j.state,
                a.manifest_json, a.created_at FROM investing_sessions s
                JOIN jobs j ON j.id=s.job_id LEFT JOIN learning_artifacts a ON a.id=s.job_id
                WHERE s.model_id=? ORDER BY j.enqueue_seq DESC''', (identity,)).fetchall()

    @staticmethod
    def _head(db, identity):
        row = db.execute('''SELECT s.job_id FROM investing_sessions s
            JOIN jobs j ON j.id=s.job_id JOIN learning_artifacts a ON a.id=s.job_id
            WHERE s.model_id=? AND j.state='succeeded' AND a.kind='policy'
            ORDER BY j.enqueue_seq DESC LIMIT 1''', (identity,)).fetchone()
        return row[0] if row else None

    def _summary(self, row, sessions):
        spec = json.loads(row['spec_json'])
        versions = [session for session in sessions if session['state'] == 'succeeded' and session['manifest_json']]
        active = next((session for session in sessions if session['state'] in ACTIVE_STATES), None)
        latest = json.loads(versions[0]['manifest_json']) if versions else {}
        state = active['state'] if active else 'ready' if versions else 'untrained'
        return dict(id=row['id'], name=spec['name'], dataset_id=row['dataset_id'], spec=spec,
                    created_at=row['created_at'], state=state, active_job_id=active['job_id'] if active else None,
                    session_count=len(sessions), version_count=len(versions),
                    total_timesteps=latest.get('timesteps_completed', 0),
                    latest_version_id=versions[0]['job_id'] if versions else None)

    def list_models(self, cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid model page size')
        offset = int(cursor or 0)
        if offset < 0:
            raise ValueError('invalid cursor')
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM investing_models ORDER BY rowid DESC LIMIT ? OFFSET ?', (limit + 1, offset)).fetchall()
        return {'items': [self._summary(row, self._sessions(row['id'])) for row in rows[:limit]],
                'next_cursor': str(offset + limit) if len(rows) > limit else None}

    def get_model(self, identity):
        row = self._model(identity)
        sessions = self._sessions(identity)
        result = self._summary(row, sessions)
        result['sessions'] = [{**self.workbench.get_resource('jobs', item['job_id']),
                               'base_version_id': item['base_version_id']} for item in sessions]
        result['versions'] = [self.detail('policies', item['job_id']) for item in sessions
                              if item['state'] == 'succeeded' and item['manifest_json']]
        return result

    def create_model(self, body):
        if not isinstance(body, dict) or set(body) != {'dataset_id', 'spec'}:
            raise ValueError('model creation requires dataset_id and spec only')
        dataset_id = _identity(body['dataset_id'], 'dataset_id')
        spec = PolicyTrainingSpec.model_validate(body['spec'])
        if not spec.name.strip():
            raise ValueError('model name must not be blank')
        ref = self._dataset(dataset_id, spec.backtest)
        from quanto.backtest.dataset_loaders import load_dataset
        from quanto.investing.training import validate_training_data
        validate_training_data(spec, load_dataset(Path(ref['path'])))
        identity = str(uuid4())
        with self.store.transaction() as db:
            db.execute('INSERT INTO investing_models VALUES (?,?,?,?)',
                       (identity, dataset_id, json_bytes(spec.model_dump(mode='json')).decode(), utc_now()))
        return self.get_model(identity)

    def train_model(self, identity, body):
        if not isinstance(body, dict) or set(body) - {'additional_timesteps', 'device'}:
            raise ValueError('training accepts additional_timesteps and device only; model configuration is fixed')
        row = self._model(identity)
        values = json.loads(row['spec_json'])
        budget = body.get('additional_timesteps', values['total_timesteps'])
        if type(budget) is not int or not 2 <= budget <= 10000000:
            raise ValueError('additional_timesteps must be an integer between 2 and 10000000')
        values.update(total_timesteps=budget, device=body.get('device', values['device']))
        spec = PolicyTrainingSpec.model_validate(values)
        with self.store.transaction() as db:
            base_version = self._head(db, identity)
        trusted = {'dataset_ref': self._dataset(row['dataset_id'], spec.backtest)}
        if base_version:
            trusted['policy_resume_ref'] = self.reference(base_version, 'policy')
            manifest = read_manifest(Path(trusted['policy_resume_ref']['path']), 'policy')
            if manifest.get('model_id') != identity or 'last.zip' not in trusted['policy_resume_ref']['files']:
                raise ValueError('model has no valid continuation state')

        def register(db, job_id):
            active = db.execute('''SELECT j.id FROM investing_sessions s JOIN jobs j ON j.id=s.job_id
                WHERE s.model_id=? AND j.state IN ('queued','running','cancel_requested')''', (identity,)).fetchone()
            if active or self._head(db, identity) != base_version:
                raise ValueError('model training conflict: another session is active or the saved version changed')
            db.execute('INSERT INTO investing_sessions VALUES (?,?,?)', (job_id, identity, base_version))

        request = {'dataset_id': row['dataset_id'], 'model_id': identity, 'base_version_id': base_version,
                   'spec': spec.model_dump(mode='json')}
        return self._submit('train_policy', request, trusted, register=register)

    def backtest_model(self, identity, body):
        self._model(identity)
        if not isinstance(body, dict) or set(body) != {'version_id', 'dataset_id', 'spec'}:
            raise ValueError('model backtest requires version_id, dataset_id and spec')
        version_id = _identity(body['version_id'], 'version_id')
        dataset_id = _identity(body['dataset_id'], 'dataset_id')
        with self.store.transaction() as db:
            owned = db.execute('''SELECT s.job_id FROM investing_sessions s JOIN learning_artifacts a ON a.id=s.job_id
                JOIN jobs j ON j.id=s.job_id WHERE s.model_id=? AND s.job_id=? AND j.state='succeeded' AND a.kind='policy' ''',
                               (identity, version_id)).fetchone()
        if owned is None:
            raise ValueError('version_id must identify a saved version belonging to this model')
        policy = self.reference(version_id, 'policy')
        manifest = read_manifest(Path(policy['path']), 'policy')
        training = PolicyTrainingSpec.model_validate(manifest['config'])
        spec = PolicyBacktestSpec.model_validate(body['spec'])
        if spec.start <= training.validation_end:
            raise ValueError('backtest must start after validation_end')
        daily = training.backtest.model_copy(update={'start': spec.start, 'end': spec.end})
        dataset = self._dataset(dataset_id, daily)
        from quanto.backtest.dataset_loaders import load_dataset
        if manifest.get('price_mode') != load_dataset(Path(dataset['path'])).price_mode:
            raise ValueError('model and backtest dataset price modes must match')
        return self._submit('policy_backtest', {'dataset_id': dataset_id, 'policy_id': version_id,
            'model_id': identity, 'spec': spec.model_dump(mode='json')}, {'dataset_ref': dataset, 'policy_ref': policy})
