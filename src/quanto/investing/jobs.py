"""Policy-specific dispatch inside the shared frozen job lifecycle."""
from pathlib import Path

from quanto.learning.registry import verify_ref
from quanto.objects import json_bytes


def execute_policy_job(kind, payload, artifact: Path, emit, cancelled):
    request = payload['request']
    dataset = verify_ref(payload['dataset_ref'])
    if kind == 'train_policy':
        from quanto.investing.training import train_policy
        train_policy(request['spec'], dataset, artifact, emit, cancelled,
                     dataset_content_hash=payload['dataset_ref']['content_hash'])
    else:
        from quanto.investing.evaluation import backtest_policy
        from quanto.learning.results import build_index
        policy = verify_ref(payload['policy_ref'])
        summary = backtest_policy(policy, dataset, request['spec'], artifact, emit, cancelled)
        summary.update(dataset_id=request['dataset_id'], policy_id=request['policy_id'],
                       dataset_content_hash=payload['dataset_ref']['content_hash'])
        (artifact / 'summary.json').write_bytes(json_bytes(summary))
        build_index(artifact, cancelled)
