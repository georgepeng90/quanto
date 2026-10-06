"""Persistent fixed-basket model identities and serialized training sessions."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from quanto.investing.contracts import CASH_ACTION_VERSION, PolicyBacktestSpec, PolicyTrainingSpec
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

    def _history_index(self, identity):
        # Read bounded metadata, never cumulative validation histories or checkpoint files.
        with self.store.transaction() as db:
            rows = db.execute('''SELECT j.id, s.base_version_id, j.enqueue_seq, j.state,
                j.created_at, j.started_at, j.finished_at, j.heartbeat, j.completed_through, j.error,
                a.kind AS artifact_kind, a.created_at AS saved_at,
                json_extract(a.manifest_json, '$.id') AS content_id,
                json_extract(a.manifest_json, '$.best_source_content_id') AS source_content_id,
                json_extract(a.manifest_json, '$.timesteps_completed') AS timesteps_completed,
                json_extract(a.manifest_json, '$.session_timesteps') AS session_timesteps,
                json_extract(a.manifest_json, '$.best_timesteps') AS best_timesteps,
                json_extract(a.manifest_json, '$.runtime.device') AS runtime_device,
                json_extract(a.manifest_json, '$.selection_valid') AS selection_valid,
                json_extract(a.manifest_json, '$.metrics.validation') AS validation_best,
                json_extract(a.manifest_json, '$.history[#-1]') AS validation_latest
                FROM investing_sessions s JOIN jobs j ON j.id=s.job_id
                LEFT JOIN learning_artifacts a ON a.id=s.job_id
                WHERE s.model_id=? ORDER BY j.enqueue_seq''', (identity,)).fetchall()
        items, by_id, by_content, number = [], {}, {}, 0
        for attempt, row in enumerate(rows, 1):
            item = dict(row)
            published = item.pop('artifact_kind') == 'policy' and item['state'] == 'succeeded'
            if published:
                number += 1
                item['selection_valid'] = bool(item['selection_valid'])
                item['validation_best'] = json.loads(item['validation_best'] or '{}')
                latest = json.loads(item['validation_latest'] or '{}')
                item['validation_latest'] = {key.removeprefix('validation_'): value
                                             for key, value in latest.items() if key.startswith('validation_')}
            item.update(session_number=attempt, round_number=number if published else None)
            items.append(item)
            by_id[item['id']] = item
            if published:
                by_content[item['content_id']] = item
        for item in items:
            base = by_id.get(item['base_version_id'])
            source = by_content.get(item.pop('source_content_id') or item['content_id'])
            item.pop('content_id')
            item['base_round_number'] = base['round_number'] if base else None
            item['best_round_number'] = source['round_number'] if source else None
            item['best_source_version_id'] = source['id'] if source else None
        return items[::-1]

    def get_model(self, identity, include_history=True):
        row = self._model(identity)
        if not include_history:
            from quanto.learning.service import capabilities

            items = self._history_index(identity)
            versions = [item for item in items if item['round_number'] is not None]
            active = next((item for item in items if item['state'] in ACTIVE_STATES), None)
            latest = versions[0] if versions else None
            spec = json.loads(row['spec_json'])
            return dict(id=identity, name=spec['name'], dataset_id=row['dataset_id'], spec=spec,
                        created_at=row['created_at'], state=active['state'] if active else 'ready' if latest else 'untrained',
                        active_job_id=active['id'] if active else None, active_session=active,
                        session_count=len(items), version_count=len(versions),
                        total_timesteps=latest['timesteps_completed'] if latest else 0,
                        latest_version_id=latest['id'] if latest else None, capabilities=capabilities())
        sessions = self._sessions(identity)
        result = self._summary(row, sessions)
        result['sessions'] = [{**self.workbench.get_resource('jobs', item['job_id']),
                               'base_version_id': item['base_version_id']} for item in sessions]
        result['versions'] = [self.detail('policies', item['job_id']) for item in sessions
                              if item['state'] == 'succeeded' and item['manifest_json']]
        history = {item['id']: item for item in self._history_index(identity)}
        for version in result['versions']:
            item = history[version['id']]
            version.update(round_number=item['round_number'], best_round_number=item['best_round_number'])
        return result

    def history(self, identity, kind='versions', page=1, page_size=10, anchor=None,
                round_number=None, session_number=None):
        self._model(identity)
        if kind not in {'versions', 'sessions'} or type(page) is not int or page < 1:
            raise ValueError('invalid history kind or page')
        if type(page_size) is not int or not 1 <= page_size <= 50:
            raise ValueError('history page size must be between 1 and 50')
        for value, minimum in ((anchor, 0), (round_number, 1), (session_number, 1)):
            if value is not None and (type(value) is not int or value < minimum):
                raise ValueError('invalid history position')
        if session_number is not None and (kind != 'sessions' or round_number is not None):
            raise ValueError('session lookup requires sessions history and no round lookup')
        items = [item for item in self._history_index(identity)
                 if kind == 'sessions' or item['round_number'] is not None]
        if anchor is None:
            anchor = items[0]['enqueue_seq'] if items else 0
        items = [item for item in items if item['enqueue_seq'] <= anchor]
        lookup = round_number if round_number is not None else session_number
        if lookup is not None:
            field = 'round_number' if round_number is not None else 'session_number'
            index = next((index for index, item in enumerate(items) if item[field] == lookup), None)
            if index is None:
                raise ValueError('requested training record does not exist in this history')
            page = index // page_size + 1
        total = len(items)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        return dict(items=items[(page - 1) * page_size:page * page_size], total=total,
                    page=page, page_size=page_size, total_pages=total_pages, anchor=anchor)

    def create_model(self, body):
        if not isinstance(body, dict) or set(body) != {'dataset_id', 'spec'}:
            raise ValueError('model creation requires dataset_id and spec only')
        dataset_id = _identity(body['dataset_id'], 'dataset_id')
        requested = {'action_version': CASH_ACTION_VERSION, **body['spec']} if isinstance(body['spec'], dict) else body['spec']
        spec = PolicyTrainingSpec.model_validate(requested)
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
