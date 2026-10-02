"""Frozen learning job dispatch. All caller paths originate from the local registry."""
from __future__ import annotations

import json
import math
from datetime import timedelta
from pathlib import Path

from quanto.backtest.models import BacktestCancelled, RunEvent
from quanto.learning.registry import LEARNING_KINDS, hash_files, verify_ref, verify_runtime
from quanto.workbench.worker import atomic_json


def expected_trading_days(manifest: dict, spec: dict) -> list[str]:
    from quanto.learning.contracts import timestamp
    start, end = timestamp(spec['start']), timestamp(spec['end'])
    interval = timedelta(minutes=manifest['frequency_minutes'])
    days = set()
    for session in manifest['trading_sessions']:
        begin, finish = timestamp(session['start']), timestamp(session['end'])
        first = begin + max(0, math.ceil((start - begin) / interval)) * interval
        if first + interval <= min(finish, end):
            days.add(session['trading_date'])
    return sorted(days)


def execute_learning_job(descriptor: dict, control_dir: Path, emit, cancelled) -> dict:
    from quanto.learning.contracts import LearningCancelled
    payload = descriptor['input']
    stage = control_dir.parent / 'staging'
    stage.mkdir()
    artifact = stage / 'artifact'
    sequence = 0

    def progress(event):
        nonlocal sequence
        sequence += 1
        if isinstance(event, RunEvent):
            emit(event)
            return
        emit(RunEvent(sequence, descriptor['kind'], str(event.get('asof', '')),
                      int(event.get('epoch', event.get('completed', 0))), int(event.get('total', 0)),
                      str(event.get('level', 'info')), str(event.get('kind', 'LEARNING_PROGRESS')),
                      str(event.get('message', '深度学习任务进行中')), event))

    try:
        verify_runtime(payload['environment'])
        kind = descriptor['kind']
        request = payload['request']
        if kind == 'prepare_learning':
            from quanto.learning.dataset import prepare_dataset
            prepare_dataset(request['spec'], Path(payload['market_root']), artifact, progress, cancelled)
        else:
            dataset = verify_ref(payload['dataset_ref'])
            if kind == 'train':
                from quanto.learning.training import train_model
                resume = Path(payload['resume_dir']) if payload.get('resume_dir') else None
                if resume and hash_files(resume) != payload['resume_files']:
                    raise ValueError('INVALID_CHECKPOINT: resume checkpoint changed')
                train_model(request['spec'], dataset, artifact, progress, cancelled, resume_dir=resume)
            else:
                from quanto.learning.dataset import iter_dataset_bars, read_dataset_manifest
                from quanto.learning.intraday import run_intraday_backtest
                manifest = read_dataset_manifest(dataset)
                spec = request['spec']
                metadata = {'dataset_id': request['dataset_id'], 'dataset_content_id': manifest['id'],
                            'frequency_minutes': manifest['frequency_minutes'], 'query': manifest['query'],
                            'limitations': manifest.get('limitations', []), 'allow_in_sample': request.get('allow_in_sample', False),
                            'price_mode': 'research', 'source_snapshot_id': manifest.get('snapshot_id')}
                metadata['expected_trading_days'] = expected_trading_days(manifest, spec)
                if manifest.get('trading_days'):
                    metadata['trading_days'] = manifest['trading_days']
                if spec.get('baseline', 'model') == 'model':
                    from quanto.learning.inference import iter_inference_rows
                    from quanto.learning.artifacts import read_model_manifest
                    model = verify_ref(payload['model_ref'])
                    model_manifest = read_model_manifest(model)
                    metadata.update(model_id=request['model_id'], model_content_id=model_manifest['id'],
                                    model_cutoff=model_manifest['cutoff'], model_config=model_manifest['config'])
                    rows = iter_inference_rows(model, dataset, spec['start'], spec['end'], cancelled)
                else:
                    rows = iter_dataset_bars(dataset, spec['start'], spec['end'])
                run_intraday_backtest(rows, spec, artifact, progress, cancelled, metadata)
                from quanto.learning.results import build_index
                build_index(artifact, cancelled)
        if cancelled():
            raise BacktestCancelled('CANCELLED: 深度学习任务已取消')
        result = {'artifact_kind': LEARNING_KINDS[kind], 'artifact_id': descriptor['job_id']}
        atomic_json(stage / 'result.json', result)
        hashes = hash_files(stage)
        if cancelled():
            raise BacktestCancelled('CANCELLED: 深度学习任务已取消')
        stage.rename(control_dir.parent / 'final')
        return {**result, 'hashes': hashes}
    except LearningCancelled as exc:
        raise BacktestCancelled(str(exc)) from exc
    except (ValueError, ImportError) as exc:
        progress({'kind': 'LEARNING_ERROR', 'level': 'error', 'message': str(exc)})
        raise


def validate_learning_output(kind: str, final: Path, result: dict) -> None:
    if kind not in LEARNING_KINDS or result.get('artifact_kind') != LEARNING_KINDS[kind]:
        raise ValueError('INVALID_OUTPUT: learning artifact kind')
    if hash_files(final) != result['hashes']:
        raise ValueError('INVALID_OUTPUT: learning artifact changed')
    saved = json.loads((final / 'result.json').read_text(encoding='utf-8'))
    if saved != {key: value for key, value in result.items() if key != 'hashes'}:
        raise ValueError('INVALID_OUTPUT: learning receipt mismatch')
    root = final / 'artifact'
    if kind == 'model_backtest':
        required = {'summary.json', 'equity.csv', 'daily_equity.csv', 'orders.csv', 'fills.csv', 'signals.csv', 'positions.csv', 'results.sqlite3'}
        if not required.issubset({path.name for path in root.iterdir()}):
            raise ValueError('INVALID_OUTPUT: incomplete intraday results')
        summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
        if not all(key in summary for key in ('metrics', 'assumptions', 'counts', 'period')):
            raise ValueError('INVALID_OUTPUT: invalid intraday summary')
    else:
        from quanto.learning.artifacts import read_manifest
        read_manifest(root, LEARNING_KINDS[kind])
