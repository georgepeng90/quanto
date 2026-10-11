"""Verified framework identity for every saved ETF policy version."""
from __future__ import annotations

import json

from quanto.framework import canonical_hash, thaw_json
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import policy_identity
from quanto.learning.artifacts import read_manifest
from quanto.learning.registry import verify_ref
from quanto.objects import digest, json_bytes
from quanto.workbench.bindings import BindingStore


def verified_policy_manifest(manifest: dict, context: dict) -> dict:
    """Join immutable artifact facts with its separately registered identity."""
    if set(context) != {"schema_version", "model_id", "artifact_model_id", "dataset_content_hash", "basket",
                        "price_mode",
                        "semantic_identity", "framework_learning"} or context["schema_version"] != 1:
        raise ValueError("MODEL_IDENTITY_MISMATCH: policy version context is invalid")
    if (manifest.get("model_id") != context["artifact_model_id"]
            or manifest.get("dataset_content_hash") != context["dataset_content_hash"]
            or manifest.get("basket") != context["basket"]):
        raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact differs from version context")
    config = PolicyTrainingSpec.model_validate(manifest["config"])
    if policy_identity(config) != context["semantic_identity"]:
        raise ValueError("MODEL_IDENTITY_MISMATCH: policy version semantics differ from training spec")
    for key, value in {**context["semantic_identity"], "price_mode": context["price_mode"],
                       "framework_learning": context["framework_learning"]}.items():
        if key in manifest and manifest[key] != value:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact semantics differ from version context")
    return {**manifest, "model_id": context["model_id"], "price_mode": context["price_mode"],
            **context["semantic_identity"],
            "framework_learning": context["framework_learning"]}


class PolicyVersionStore:
    def __init__(self, store):
        self.store = store

    def context_for(self, binding, manifest: dict, version_id: str | None = None) -> dict:
        from quanto.investing.feature_runtime import learning_runtime_for

        spec = PolicyTrainingSpec.model_validate(manifest["config"])
        original = PolicyTrainingSpec.model_validate(thaw_json(binding.original_spec))
        if (spec.model_dump(mode="json", exclude={"total_timesteps", "device"}) !=
                original.model_dump(mode="json", exclude={"total_timesteps", "device"})):
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy research configuration differs from model")
        if policy_identity(spec) != thaw_json(binding.semantic_identity):
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy semantic version differs from model")
        runtime = learning_runtime_for(binding, binding.training_configuration)
        prices = next((item for item in binding.asset_binding.table_semantics
                       if item.table == "prices"), None)
        if prices is None:
            raise ValueError("MODEL_IDENTITY_MISMATCH: model price semantics missing")
        artifact_model_id = manifest.get("model_id")
        if artifact_model_id != binding.model_id:
            adoptions = binding.evidence.get("standalone_adoptions", {})
            adoption = adoptions.get(manifest.get("id"))
            if (artifact_model_id is not None or adoption is None
                    or (version_id is not None and adoption.get("version_id") != version_id)):
                raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact belongs to another model")
        context = {"schema_version": 1, "model_id": binding.model_id,
                   "artifact_model_id": artifact_model_id,
                   "dataset_content_hash": binding.asset_binding.dataset_content_hash,
                   "basket": list(spec.backtest.symbols),
                   "price_mode": prices.temporal_rules["price_mode"],
                   "semantic_identity": policy_identity(spec),
                   "framework_learning": runtime.identity(spec)}
        verified_policy_manifest(manifest, context)
        return context

    def insert(self, db, version_id: str, context: dict) -> dict:
        row = db.execute("SELECT kind,path,content_hash,files_json,manifest_json FROM learning_artifacts WHERE id=?",
                         (version_id,)).fetchone()
        if row is None or row["kind"] != "policy":
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy version artifact missing")
        files = json.loads(row["files_json"])
        if digest(json_bytes(files)) != row["content_hash"]:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact registry hash differs")
        manifest = read_manifest(verify_ref({"path": row["path"], "files": files}), "policy")
        if manifest != json.loads(row["manifest_json"]):
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact registry differs from files")
        binding = BindingStore(self.store).get_model_binding(context["model_id"], db)
        if self.context_for(binding, manifest, version_id) != context:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy context differs from bound model")
        if manifest.get("framework_learning") is None or manifest.get("price_mode") is None:
            raise ValueError("MODEL_IDENTITY_MISMATCH: current policy manifest lacks framework identity")
        return self._persist_verified_context(db, version_id, context, row["content_hash"])

    def _persist_verified_context(self, db, version_id: str, context: dict,
                                  artifact_content_hash: str) -> dict:
        serialized = json_bytes(context).decode()
        identity_hash = canonical_hash(context)
        previous = db.execute("SELECT context_json,context_hash,artifact_content_hash FROM policy_version_contexts "
                              "WHERE version_id=?", (version_id,)).fetchone()
        if previous is not None:
            if (json.loads(previous["context_json"]) != context or previous["context_hash"] != identity_hash
                    or previous["artifact_content_hash"] != artifact_content_hash):
                raise ValueError("MODEL_IDENTITY_MISMATCH: policy version context conflict")
            return context
        db.execute("INSERT INTO policy_version_contexts VALUES (?,?,?,?)",
                   (version_id, serialized, identity_hash, artifact_content_hash))
        return context

    def get(self, version_id: str, db=None) -> dict:
        if db is None:
            with self.store.transaction() as connection:
                return self.get(version_id, connection)
        row = db.execute("SELECT context_json,context_hash,artifact_content_hash FROM policy_version_contexts "
                         "WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy version context missing")
        context = json.loads(row["context_json"])
        if canonical_hash(context) != row["context_hash"]:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy version context hash differs")
        artifact = db.execute("SELECT kind,path,content_hash,files_json,manifest_json FROM learning_artifacts WHERE id=?",
                              (version_id,)).fetchone()
        if artifact is None or artifact["kind"] != "policy" or artifact["content_hash"] != row["artifact_content_hash"]:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy version artifact changed")
        files = json.loads(artifact["files_json"])
        if digest(json_bytes(files)) != artifact["content_hash"]:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact registry hash differs")
        manifest = read_manifest(verify_ref({"path": artifact["path"], "files": files}), "policy")
        if manifest != json.loads(artifact["manifest_json"]):
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact registry differs from files")
        binding = BindingStore(self.store).get_model_binding(context["model_id"], db)
        if self.context_for(binding, manifest, version_id) != context:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy version context differs from model")
        return context
