"""One verified business context for every fixed or policy backtest."""
from __future__ import annotations

import json
import hashlib
from collections.abc import Mapping
from pathlib import Path

from quanto.framework import AssetBinding, BasketRevisionRef, canonical_hash, thaw_json
from quanto.workbench.results import _execution_conditions


def create_context(*, kind: str, dataset_id: str, asset_binding: AssetBinding,
                   ordered_symbols: list[str] | tuple[str, ...], backtest_spec: dict,
                   execution_provenance: dict, input_hash: str,
                   basket_revision_ref: BasketRevisionRef | dict,
                   name: str, method: str, dataset_source: str | None = None,
                   sample_purpose: str | None = None, evaluation_purpose: str | None = None,
                   definition_evidence: dict | None = None, strategy_revision_id: str | None = None,
                   model_id: str | None = None, policy_version_id: str | None = None,
                   baseline: str | None = None) -> dict:
    """Project facts from frozen inputs; never infer a historical engine definition."""
    if kind not in {"fixed", "policy"} or not name or not method:
        raise ValueError("invalid backtest context kind or display name")
    if not isinstance(asset_binding, AssetBinding) or asset_binding.dataset_version_id != dataset_id:
        raise ValueError("backtest context dataset binding differs")
    if not isinstance(input_hash, str) or len(input_hash) != 64 or any(c not in "0123456789abcdef" for c in input_hash):
        raise ValueError("backtest context input hash is invalid")
    if not isinstance(backtest_spec, dict) or not isinstance(execution_provenance, dict):
        raise ValueError("backtest context needs frozen spec and execution evidence")
    symbols = tuple(ordered_symbols)
    if not symbols or tuple(backtest_spec.get("symbols", ())) != symbols or len(set(symbols)) != len(symbols):
        raise ValueError("backtest context symbols differ from frozen spec")
    by_local = {item.local_symbol: item for item in asset_binding.assets}
    try:
        pairs = [by_local[symbol] for symbol in symbols]
    except KeyError as exc:
        raise ValueError("backtest context symbol lacks a verified asset identity") from exc
    ref = BasketRevisionRef.model_validate(basket_revision_ref)
    source_hashes = execution_provenance.get("source_hashes")
    environment_fingerprint = execution_provenance.get("environment_fingerprint")
    if not isinstance(source_hashes, dict) or not source_hashes or not isinstance(environment_fingerprint, str):
        raise ValueError("backtest context execution evidence is incomplete")
    execution = _execution_conditions(execution_provenance)
    spec = thaw_json(backtest_spec)
    benchmark = asset_binding.benchmark
    benchmark_identity = benchmark.instrument_id.key if benchmark.instrument_id else benchmark.custom_series_id
    ordered_pairs = [{"instrument_id": item.instrument_id.key, "local_symbol": item.local_symbol,
                      "evidence_ref": item.evidence_ref} for item in pairs]
    table_semantics = [{key: getattr(item, key) for key in
                        ("table", "semantic_id", "version", "schema_definition", "temporal_rules")}
                       for item in asset_binding.table_semantics]
    definitions = thaw_json(definition_evidence or {})
    trading_semantics = (definitions.get("trading") or
        {key: definitions[key] for key in
         ("execution_definition_version", "risk_definition_version", "execution_version")
         if key in definitions})
    conditions = {"ordered_instruments": [item["instrument_id"] for item in ordered_pairs],
                  "instruments": sorted(item["instrument_id"] for item in ordered_pairs),
                  "benchmark": {"identity": benchmark_identity, "return_basis": benchmark.return_basis},
                  "dataset_content_hash": asset_binding.dataset_content_hash,
                  "table_semantics": sorted(thaw_json(table_semantics), key=lambda item: item["table"]),
                  "trading_semantics": trading_semantics,
                  "period": {"start": spec.get("start"), "end": spec.get("end")},
                  "execution": spec.get("execution"), "risk": spec.get("risk"),
                  "initial_cash": spec.get("initial_cash"), "price_mode": next(
                      (item.temporal_rules.get("price_mode") for item in asset_binding.table_semantics
                       if item.table == "prices"), None),
                  "metrics": spec.get("metrics"), "sample_purpose": sample_purpose,
                  "evaluation_purpose": evaluation_purpose,
                  "shared_execution": execution}
    return {"schema_version": 1, "kind": kind, "name": name, "method": method,
            "dataset_version_id": dataset_id, "dataset_content_hash": asset_binding.dataset_content_hash,
            "dataset_source": dataset_source, "asset_binding_hash": asset_binding.binding_hash,
            "basket_revision_ref": ref.model_dump(mode="json"), "ordered_pairs": ordered_pairs,
            "canonical_assets": conditions["instruments"],
            "asset_types": sorted({item.instrument_id.instrument_type for item in pairs}),
            "benchmark_identity": conditions["benchmark"], "backtest_spec": spec,
            "sample_purpose": sample_purpose, "evaluation_purpose": evaluation_purpose,
            "definition_evidence": definitions,
            "strategy_revision_id": strategy_revision_id, "model_id": model_id,
            "policy_version_id": policy_version_id, "baseline": baseline,
            "input_hash": input_hash, "execution_provenance": thaw_json(execution_provenance),
            "conditions": conditions,
            "comparison_key": canonical_hash(conditions) if execution is not None else None}


def fixed_context_from_manifest(manifest: dict, *, input_hash: str,
                                asset_binding: AssetBinding,
                                basket_revision_ref: BasketRevisionRef | dict) -> dict:
    """Build a fixed run context from a current frozen framework manifest."""
    resolved = manifest["resolved_configuration"]
    spec = manifest["backtest_spec"]
    definitions = {item["ref"]["kind"]: {key: item[key] for key in
                   ("ref", "implementation_hash", "effective_params")}
                   for item in resolved["definitions"]}
    strategy = manifest["strategy_manifest"]
    name = strategy.get("name") or strategy["entrypoint"]
    from quanto.framework import canonical_hash

    return create_context(kind="fixed", dataset_id=manifest["dataset_version_id"],
                          asset_binding=asset_binding, ordered_symbols=spec["symbols"],
                          backtest_spec=spec, basket_revision_ref=basket_revision_ref,
                          execution_provenance={"source_hashes": manifest["files"],
                                                "environment_fingerprint": canonical_hash(manifest["environment"])},
                          input_hash=input_hash, name=str(name), method=str(name),
                          dataset_source=manifest["dataset_manifest"].get("source"),
                          sample_purpose=manifest.get("sample_purpose"),
                          definition_evidence=definitions,
                          strategy_revision_id=manifest["strategy_revision_id"])


class ContextStore:
    def __init__(self, store):
        self.store = store

    def insert(self, db, job_id: str, context: Mapping) -> dict:
        value = dict(context)
        if value.get("schema_version") != 1 or value.get("kind") not in {"fixed", "policy"}:
            raise ValueError("invalid backtest context")
        job = db.execute("SELECT kind FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None or job[0] != {"fixed": "backtest", "policy": "policy_backtest"}[value["kind"]]:
            raise ValueError("backtest context job kind differs")
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = canonical_hash(value)
        previous = db.execute("SELECT context_hash FROM run_contexts WHERE job_id=?", (job_id,)).fetchone()
        if previous is not None:
            if previous[0] != digest:
                raise ValueError("backtest context conflict")
            return self.get(job_id, db)
        db.execute("INSERT INTO run_contexts (job_id,context_json,context_hash) VALUES (?,?,?)",
                   (job_id, encoded, digest))
        return value

    def get(self, job_id: str, db=None) -> dict:
        if db is None:
            with self.store.transaction() as connection:
                return self.get(job_id, connection)
        row = db.execute("SELECT context_json,context_hash FROM run_contexts WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"backtest context missing for job {job_id}")
        value = json.loads(row["context_json"])
        if canonical_hash(value) != row["context_hash"]:
            raise ValueError("backtest context hash differs")
        source = db.execute("""SELECT j.kind,j.input_path,r.manifest_path,r.manifest_hash,
                             s.input_hash AS submission_hash FROM jobs j
                             LEFT JOIN runs r ON r.job_id=j.id
                             LEFT JOIN learning_submissions s ON s.job_id=j.id
                             WHERE j.id=?""", (job_id,)).fetchone()
        if source is None or source["kind"] != {"fixed": "backtest", "policy": "policy_backtest"}[value["kind"]]:
            raise ValueError("backtest context job differs")
        path = source["manifest_path"] if value["kind"] == "fixed" else source["input_path"]
        expected = source["manifest_hash"] if value["kind"] == "fixed" else source["submission_hash"]
        if not path or expected != value["input_hash"]:
            raise ValueError("backtest context frozen input reference differs")
        with Path(path).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("backtest context frozen input changed")
        return value
