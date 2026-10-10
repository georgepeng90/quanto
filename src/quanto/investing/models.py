"""Persistent fixed-basket model identities and serialized training sessions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from quanto.framework import ModelFrameworkBinding, thaw_json
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import policy_identity
from quanto.investing.service import InvestingService
from quanto.objects import json_bytes
from quanto.workbench.bindings import BindingStore
from quanto.workbench.configuration import FrameworkResolver, recheck_unchanged
from quanto.workbench.framework_requests import ModelCreateRequest, PolicyBacktestRequest, ResumeRequest
from quanto.workbench.store import utc_now


ACTIVE_STATES = {'queued', 'running', 'cancel_requested'}


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
                json_extract(a.manifest_json, '$.training_sampling') AS training_sampling,
                json_extract(a.manifest_json, '$.metrics.validation') AS validation_best,
                json_extract(a.manifest_json, '$.history[#-1]') AS validation_latest
                FROM investing_sessions s JOIN jobs j ON j.id=s.job_id
                LEFT JOIN learning_artifacts a ON a.id=s.job_id
                WHERE s.model_id=? ORDER BY j.enqueue_seq''', (identity,)).fetchall()
        items, by_id, by_content, number = [], {}, {}, 0
        for attempt, row in enumerate(rows, 1):
            item = dict(row)
            sampling = json.loads(item['training_sampling']) if item['training_sampling'] else None
            item['training_sampling'] = sampling if isinstance(sampling, dict) else None
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

    def _session_sampling(self, item):
        if item is None or item['round_number'] is not None:
            return
        path = self.workbench._job_root(item['id']) / 'control' / 'training-sampling.json'
        try:
            sampling = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        item['training_sampling'] = sampling if isinstance(sampling, dict) else None

    def get_model(self, identity, include_history=True):
        row = self._model(identity)
        if not include_history:
            from quanto.learning.service import capabilities

            items = self._history_index(identity)
            versions = [item for item in items if item['round_number'] is not None]
            active = next((item for item in items if item['state'] in ACTIVE_STATES), None)
            self._session_sampling(active)
            latest = versions[0] if versions else None
            spec = json.loads(row['spec_json'])
            result = dict(id=identity, name=spec['name'], dataset_id=row['dataset_id'], spec=spec,
                        created_at=row['created_at'], state=active['state'] if active else 'ready' if latest else 'untrained',
                        active_job_id=active['id'] if active else None, active_session=active,
                        session_count=len(items), version_count=len(versions),
                        total_timesteps=latest['timesteps_completed'] if latest else 0,
                        latest_version_id=latest['id'] if latest else None, capabilities=capabilities())
            return self._with_framework(result)
        sessions = self._sessions(identity)
        result = self._summary(row, sessions)
        result['sessions'] = [{**self.workbench.get_resource('jobs', item['job_id']),
                               'base_version_id': item['base_version_id']} for item in sessions]
        result['versions'] = [self.detail('policies', item['job_id']) for item in sessions
                              if item['state'] == 'succeeded' and item['manifest_json']]
        return self._with_framework(result)

    def _with_framework(self, result):
        bindings = BindingStore(self.store)
        try:
            binding = bindings.get_model_binding(result['id'])
        except (KeyError, ValueError):
            result['framework_binding'] = None
            result['framework_binding_check'] = bindings.inspect_model_binding(result['id']).model_dump(mode='json')
        else:
            result['framework_binding'] = dict(model_id=binding.model_id, origin=binding.origin,
                binding_hash=binding.binding_hash, original_spec_hash=binding.original_spec_hash,
                basket_revision_ref=binding.basket.ref.model_dump(mode='json'),
                ordered_pairs=[item.model_dump(mode='json') for item in binding.ordered_pairs],
                definitions=[item.model_dump(mode='json') for item in binding.training_configuration.definitions],
                evidence=thaw_json(binding.evidence))
            result['framework_binding_check'] = None
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
        visible = items[(page - 1) * page_size:page * page_size]
        for item in visible:
            self._session_sampling(item)
        return dict(items=visible, total=total,
                    page=page, page_size=page_size, total_pages=total_pages, anchor=anchor)

    def create_model(self, body):
        request = ModelCreateRequest.model_validate(body)
        resolved = FrameworkResolver(self.store).resolve(request)
        spec = PolicyTrainingSpec.model_validate(thaw_json(resolved.internal_training_spec))
        if not spec.name.strip():
            raise ValueError('model name must not be blank')
        identity = str(uuid4())
        binding = ModelFrameworkBinding(model_id=identity, origin='new', original_spec=spec.model_dump(mode='json'),
            training_configuration=resolved, semantic_identity=policy_identity(spec),
            evidence={'method': 'resolved-new-model', 'request_hash': resolved.request_hash},
            created_at=datetime.now(timezone.utc))
        with self.store.transaction() as db:
            recheck_unchanged(resolved, lambda: FrameworkResolver(self.store).resolve(request, db=db))
            db.execute('INSERT INTO investing_models VALUES (?,?,?,?)',
                       (identity, request.dataset_id, json_bytes(spec.model_dump(mode='json')).decode(), utc_now()))
            BindingStore(self.store)._insert_model_binding(db, binding)
        return self.get_model(identity)

    def train_model(self, identity, body):
        if not isinstance(body, dict) or 'model_id' in body:
            raise ValueError('training accepts additional_timesteps and device only')
        self._model(identity)
        operation = ResumeRequest.model_validate({'model_id': identity, **body})
        resolved = FrameworkResolver(self.store).resolve(operation)
        binding = BindingStore(self.store).get_model_binding(identity)
        spec = PolicyTrainingSpec.model_validate(thaw_json(resolved.internal_training_spec))
        base_version = resolved.capability_report.input_hashes['parent_version']
        trusted = {'dataset_ref': self._dataset(binding.dataset_version_id, spec.backtest),
                   'resolved_configuration': resolved.model_dump(mode='json'),
                   'model_binding': binding.model_dump(mode='json')}
        trusted['training_dataset_ref'] = trusted['dataset_ref']
        if base_version:
            trusted['policy_resume_ref'] = self.reference(base_version, 'policy')

        def register(db, job_id):
            active = db.execute('''SELECT j.id FROM investing_sessions s JOIN jobs j ON j.id=s.job_id
                WHERE s.model_id=? AND j.state IN ('queued','running','cancel_requested')''', (identity,)).fetchone()
            if active or self._head(db, identity) != base_version:
                raise ValueError('model training conflict: another session is active or the saved version changed')
            if BindingStore(self.store).get_model_binding(identity, db) != binding:
                raise ValueError('MODEL_IDENTITY_MISMATCH: model binding changed before submission')
            db.execute('INSERT INTO investing_sessions VALUES (?,?,?)', (job_id, identity, base_version))

        request = {'dataset_id': binding.dataset_version_id, 'model_id': identity, 'base_version_id': base_version,
                   'spec': spec.model_dump(mode='json')}
        return self._submit('train_policy', request, trusted, register=register, resolved=resolved,
                            recheck=lambda db: FrameworkResolver(self.store).resolve(operation, db=db))

    def backtest_model(self, identity, body):
        self._model(identity)
        request = PolicyBacktestRequest.model_validate(body)
        with self.store.transaction() as db:
            owned = db.execute('''SELECT s.job_id FROM investing_sessions s JOIN learning_artifacts a ON a.id=s.job_id
                JOIN jobs j ON j.id=s.job_id WHERE s.model_id=? AND s.job_id=? AND j.state='succeeded' AND a.kind='policy' ''',
                               (identity, request.version_id)).fetchone()
        if owned is None:
            raise ValueError('version_id must identify a saved version belonging to this model')
        resolved = FrameworkResolver(self.store).resolve(request)
        binding = BindingStore(self.store).get_model_binding(identity)
        from quanto.config import BacktestSpec
        daily = BacktestSpec.model_validate(thaw_json(resolved.internal_backtest_spec))
        dataset = self._dataset(request.dataset_id, daily)
        original = PolicyTrainingSpec.model_validate(thaw_json(binding.original_spec))
        return self._submit('policy_backtest', {'dataset_id': request.dataset_id, 'policy_id': request.version_id,
            'model_id': identity, 'spec': request.spec.model_dump(mode='json')},
            {'dataset_ref': dataset, 'policy_ref': self.reference(request.version_id, 'policy'),
             'training_dataset_ref': self._dataset(binding.dataset_version_id, original.backtest),
             'resolved_configuration': resolved.model_dump(mode='json'), 'model_binding': binding.model_dump(mode='json')},
            resolved=resolved, recheck=lambda db: FrameworkResolver(self.store).resolve(request, db=db))
