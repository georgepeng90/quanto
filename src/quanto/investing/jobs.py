"""Policy-specific dispatch inside the shared frozen job lifecycle."""
from pathlib import Path

from quanto.learning.registry import verify_ref
from quanto.objects import json_bytes


def _learning_runtime(kind, payload):
    from quanto.framework import ModelFrameworkBinding, ResolvedConfiguration, thaw_json
    from quanto.investing.feature_runtime import learning_runtime_for
    from quanto.investing.framework import validate_model_operation
    from quanto.workbench.framework_requests import PolicyBacktestRequest, ResumeRequest

    if 'model_binding' not in payload or 'resolved_configuration' not in payload:
        raise ValueError('MODEL_IDENTITY_MISMATCH: policy job requires frozen model binding and configuration')
    binding = ModelFrameworkBinding.model_validate(payload['model_binding'])
    resolved = ResolvedConfiguration.model_validate(payload['resolved_configuration'])
    request = payload['request']
    dataset = payload['dataset_ref']
    if (resolved.capability_report.status != 'ready' or request.get('model_id') != binding.model_id
            or dataset['id'] != resolved.asset_binding.dataset_version_id
            or request['dataset_id'] != dataset['id']
            or dataset['content_hash'] != resolved.asset_binding.dataset_content_hash):
        raise ValueError('MODEL_IDENTITY_MISMATCH: job source differs from frozen configuration')
    if kind == 'train_policy':
        operation = ResumeRequest.model_validate(thaw_json(resolved.request))
        if (request['spec'] != thaw_json(resolved.internal_training_spec)
                or request.get('base_version_id') != resolved.capability_report.input_hashes.get('parent_version')
                or (payload.get('policy_resume_ref') or {}).get('id') != request.get('base_version_id')):
            raise ValueError('MODEL_IDENTITY_MISMATCH: training request or parent differs from frozen configuration')
    else:
        operation = PolicyBacktestRequest.model_validate(thaw_json(resolved.request))
        if (request['spec'] != operation.spec.model_dump(mode='json')
                or request['policy_id'] != operation.version_id
                or payload['policy_ref']['id'] != operation.version_id):
            raise ValueError('MODEL_IDENTITY_MISMATCH: backtest request or version differs from frozen configuration')
    validate_model_operation(binding, operation, resolved)
    return learning_runtime_for(binding, resolved)


def execute_policy_job(kind, payload, artifact: Path, emit, cancelled):
    runtime = _learning_runtime(kind, payload)
    request = payload['request']
    dataset = verify_ref(payload['dataset_ref'])
    if kind == 'train_policy':
        from quanto.investing.training import train_policy
        resume = verify_ref(payload['policy_resume_ref']) if payload.get('policy_resume_ref') else None
        train_policy(request['spec'], dataset, artifact, emit, cancelled,
                     dataset_content_hash=payload['dataset_ref']['content_hash'], resume_dir=resume,
                     model_id=request['model_id'], evaluate_test=False, learning_runtime=runtime)
    else:
        from quanto.investing.evaluation import backtest_policy
        from quanto.learning.results import build_index
        policy = verify_ref(payload['policy_ref'])
        summary = backtest_policy(policy, dataset, request['spec'], artifact, emit, cancelled,
                                  learning_runtime=runtime)
        summary.update(dataset_id=request['dataset_id'], policy_id=request['policy_id'],
                       dataset_content_hash=payload['dataset_ref']['content_hash'])
        if request.get('model_id'):
            summary['model_id'] = request['model_id']
        (artifact / 'summary.json').write_bytes(json_bytes(summary))
        build_index(artifact, cancelled)
