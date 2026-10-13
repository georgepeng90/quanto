"""Policy-specific dispatch inside the shared frozen job lifecycle."""
import csv
import os
from pathlib import Path

from quanto.framework import ResolvedConfiguration, canonical_hash
from quanto.learning.artifacts import read_manifest, write_manifest
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


def _execution_provenance(resolved, payload, source_hashes, *, policy_version_id=None, policy_manifest=None):
    spec = resolved.internal_training_spec or {}
    boundaries = {key: spec.get(key) for key in ("train_start", "train_end", "validation_start",
                                               "validation_end", "test_start", "test_end")}
    provenance = {
        "source_hashes": source_hashes,
        "environment_fingerprint": canonical_hash(payload["execution_environment"]),
        "model_id": payload["request"]["model_id"], "policy_version_id": policy_version_id,
        "training_boundaries": boundaries, "basket_selection_note": resolved.basket.selection_note,
        "best_checkpoint": None, "last_checkpoint": None,
        "feature_transform": policy_manifest.get("feature_transform") if policy_manifest else None,
        "training_updates": (policy_manifest["history"][-1]["updates"]
                             if policy_manifest and policy_manifest.get("history") else None),
    }
    if resolved.basket.selection_origin is not None:
        provenance['selection_origin'] = resolved.basket.selection_origin.model_dump(mode='json')
        provenance['selection_evaluation'] = 'development_research_not_historical_selection_validation'
    if policy_manifest is not None:
        ancestor = policy_manifest.get("best_source_content_id")
        provenance["best_checkpoint"] = {
            "source": "ancestor" if ancestor else "current_artifact",
            "source_content_id": ancestor or (policy_manifest["id"] if policy_version_id else None),
            "timesteps": policy_manifest["best_timesteps"],
        }
        if "last.zip" in policy_manifest["files"]:
            provenance["last_checkpoint"] = {
                "source": "current_artifact", "source_content_id": policy_manifest["id"] if policy_version_id else None,
                "cumulative_timesteps": policy_manifest["timesteps_completed"],
                "session_timesteps": policy_manifest["session_timesteps"],
            }
    return provenance


def _canonical_csv_rows(root, resolved):
    by_symbol = {pair.local_symbol: pair.instrument_id.key for pair in resolved.ordered_pairs}
    for path in root.glob("*.csv"):
        temporary = path.with_suffix(".canonical.tmp")
        with path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            if "symbol" not in (reader.fieldnames or ()):
                continue
            columns = list(reader.fieldnames)
            columns.insert(columns.index("symbol") + 1, "instrument_id")
            with temporary.open("w", encoding="utf-8", newline="") as target:
                writer = csv.DictWriter(target, columns)
                writer.writeheader()
                for row in reader:
                    symbol = row["symbol"]
                    if symbol and symbol not in by_symbol:
                        raise ValueError(f"BINDING_MISMATCH: result contains unbound symbol {symbol}")
                    row["instrument_id"] = by_symbol[symbol] if symbol else ""
                    writer.writerow(row)
        os.replace(temporary, path)


def execute_policy_job(kind, payload, artifact: Path, emit, cancelled, *, source_hashes):
    runtime = _learning_runtime(kind, payload)
    resolved = ResolvedConfiguration.model_validate(payload['resolved_configuration'])
    request = payload['request']
    dataset = verify_ref(payload['dataset_ref'])
    if kind == 'train_policy':
        from quanto.investing.training import train_policy
        resume = verify_ref(payload['policy_resume_ref']) if payload.get('policy_resume_ref') else None
        if resume is not None and 'policy_resume_context' not in payload:
            raise ValueError('MODEL_IDENTITY_MISMATCH: saved continuation context missing')
        train_policy(request['spec'], dataset, artifact, emit, cancelled,
                     dataset_content_hash=payload['dataset_ref']['content_hash'], resume_dir=resume,
                     resume_context=payload.get('policy_resume_context'), model_id=request['model_id'],
                     evaluate_test=False, learning_runtime=runtime)
        saved = read_manifest(artifact, 'policy')
        if resolved.basket.selection_origin is not None:
            (artifact / 'screening.json').write_bytes(json_bytes(payload['screening_record']))
        # The first manifest is still staging. Recompute its identity once with
        # provenance included; the returned ID must not enter its own hash input.
        content = {key: value for key, value in saved.items() if key not in {'id', 'files'}}
        content['framework'] = payload['resolved_configuration']
        content['execution_provenance'] = _execution_provenance(resolved, payload, source_hashes,
                                                                policy_manifest=saved)
        write_manifest(artifact, content)
    else:
        from quanto.investing.evaluation import backtest_policy
        from quanto.learning.results import build_index
        policy = verify_ref(payload['policy_ref'])
        if 'policy_context' not in payload:
            raise ValueError('MODEL_IDENTITY_MISMATCH: saved policy context missing')
        policy_manifest = read_manifest(policy, 'policy')
        summary = backtest_policy(policy, dataset, request['spec'], artifact, emit, cancelled,
                                  learning_runtime=runtime, policy_context=payload['policy_context'])
        summary.update(dataset_id=request['dataset_id'], policy_id=request['policy_id'],
                       dataset_content_hash=payload['dataset_ref']['content_hash'])
        if request.get('model_id'):
            summary['model_id'] = request['model_id']
        summary['framework'] = payload['resolved_configuration']
        if resolved.basket.selection_origin is not None:
            (artifact / 'screening.json').write_bytes(json_bytes(payload['screening_record']))
            summary['assumptions']['limitations'].append(
                f"Screening as of {resolved.basket.selection_origin.as_of.isoformat()}; "
                "this development backtest does not validate historical stock selection.")
        summary['execution_provenance'] = _execution_provenance(
            resolved, payload, source_hashes, policy_version_id=request['policy_id'],
            policy_manifest=policy_manifest)
        _canonical_csv_rows(artifact, resolved)
        (artifact / 'summary.json').write_bytes(json_bytes(summary))
        build_index(artifact, cancelled)
