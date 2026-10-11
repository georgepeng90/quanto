"""Immutable binding persistence; callers must establish identity evidence first."""
from __future__ import annotations

import json

from quanto.framework import (AssetBinding, ModelFrameworkBinding, canonical_hash, thaw_json)
from quanto.objects import digest
from quanto.workbench.store import WorkbenchStore


class BindingStore:
    def __init__(self, store: WorkbenchStore):
        self.store = store

    def get_asset_binding(self, dataset_id: str, db=None) -> AssetBinding:
        if db is None:
            with self.store.transaction() as connection:
                return self.get_asset_binding(dataset_id, connection)
        row = db.execute("SELECT * FROM dataset_asset_bindings WHERE dataset_version_id=?", (dataset_id,)).fetchone()
        if row is None:
            raise KeyError(dataset_id)
        binding = AssetBinding.model_validate_json(row["binding_json"])
        dataset = self.store.get_dataset(dataset_id, db)
        if (binding.dataset_version_id != dataset_id or binding.binding_hash != row["binding_hash"]
                or binding.dataset_content_hash != row["dataset_content_hash"] or binding.dataset_content_hash != dataset.content_hash):
            raise ValueError("BINDING_MISMATCH: stored binding differs from dataset or immutable content")
        return binding

    def _insert_asset_binding(self, db, binding: AssetBinding) -> AssetBinding:
        dataset = self.store.get_dataset(binding.dataset_version_id, db)
        if binding.dataset_content_hash != dataset.content_hash:
            raise ValueError("BINDING_MISMATCH: binding dataset content hash differs")
        row = db.execute("SELECT 1 FROM dataset_asset_bindings WHERE dataset_version_id=?", (binding.dataset_version_id,)).fetchone()
        if row is not None:
            existing = self.get_asset_binding(binding.dataset_version_id, db)
            if existing != binding:
                raise ValueError("dataset binding conflict")
            return existing
        db.execute("INSERT INTO dataset_asset_bindings VALUES (?,?,?,?)",
                   (binding.dataset_version_id, binding.dataset_content_hash, binding.model_dump_json(), binding.binding_hash))
        return binding

    def get_model_binding(self, model_id: str, db=None) -> ModelFrameworkBinding:
        if db is None:
            with self.store.transaction() as connection:
                return self.get_model_binding(model_id, connection)
        row = db.execute("SELECT * FROM model_framework_bindings WHERE model_id=?", (model_id,)).fetchone()
        if row is None:
            if db.execute("SELECT 1 FROM investing_models WHERE id=?", (model_id,)).fetchone():
                raise ValueError("BINDING_MISMATCH: model framework binding missing")
            raise KeyError(model_id)
        binding = ModelFrameworkBinding.model_validate_json(row["binding_json"])
        model = db.execute("SELECT dataset_id,spec_json FROM investing_models WHERE id=?", (model_id,)).fetchone()
        if model is None:
            raise ValueError("BINDING_MISMATCH: model row missing")
        original_spec = json.loads(model["spec_json"])
        asset = self.get_asset_binding(binding.dataset_version_id, db)
        from quanto.workbench.baskets import BasketStore
        basket = BasketStore(self.store).get(binding.basket.ref, db)
        if (binding.model_id != model_id or binding.binding_hash != row["binding_hash"]
                or binding.original_spec_hash != row["spec_hash"]
                or binding.original_spec_hash != canonical_hash(original_spec)
                or thaw_json(binding.original_spec) != original_spec
                or binding.dataset_version_id != model["dataset_id"]
                or binding.dataset_version_id != row["dataset_version_id"]
                or asset != binding.asset_binding or basket != binding.basket
                or binding.asset_binding.dataset_content_hash != row["dataset_content_hash"]
                or binding.basket.basket_id != row["basket_id"] or binding.basket.revision != row["basket_revision"]
                or binding.asset_binding.binding_hash != row["asset_binding_hash"]):
            raise ValueError("BINDING_MISMATCH: stored model binding differs from immutable references")
        self._verify_model_identity(binding, model["spec_json"])
        return binding

    def _insert_model_binding(self, db, binding: ModelFrameworkBinding) -> ModelFrameworkBinding:
        binding = ModelFrameworkBinding.model_validate(binding.model_dump(mode="json"))
        model = db.execute("SELECT dataset_id,spec_json FROM investing_models WHERE id=?", (binding.model_id,)).fetchone()
        if model is None:
            raise ValueError("BINDING_MISMATCH: model does not exist")
        asset = self.get_asset_binding(binding.dataset_version_id, db)
        from quanto.workbench.baskets import BasketStore
        basket = BasketStore(self.store).get(binding.basket.ref, db)
        if (model["dataset_id"] != binding.dataset_version_id or asset != binding.asset_binding
                or basket != binding.basket or canonical_hash(json.loads(model["spec_json"])) != binding.original_spec_hash
                or json.loads(model["spec_json"]) != thaw_json(binding.original_spec)):
            raise ValueError("BINDING_MISMATCH: model binding source references changed")
        self._verify_model_identity(binding, model["spec_json"])
        previous = db.execute("SELECT 1 FROM model_framework_bindings WHERE model_id=?", (binding.model_id,)).fetchone()
        if previous is not None:
            existing = self.get_model_binding(binding.model_id, db)
            if existing != binding:
                raise ValueError("model framework binding conflict")
            return existing
        db.execute("INSERT INTO model_framework_bindings VALUES (?,?,?,?,?,?,?,?,?)",
                   (binding.model_id, binding.original_spec_hash, binding.dataset_version_id,
                    binding.asset_binding.dataset_content_hash, binding.basket.basket_id,
                    binding.basket.revision, binding.asset_binding.binding_hash,
                    binding.model_dump_json(), binding.binding_hash))
        return binding

    @staticmethod
    def _verify_model_identity(binding: ModelFrameworkBinding, raw_spec: str) -> None:
        from quanto.investing.contracts import PolicyTrainingSpec
        from quanto.investing.evaluation import policy_identity

        original = PolicyTrainingSpec.model_validate(json.loads(raw_spec))
        if thaw_json(binding.training_configuration.internal_training_spec) != original.model_dump(mode="json"):
            raise ValueError("MODEL_IDENTITY_MISMATCH: resolved training spec differs from original model")
        if thaw_json(binding.semantic_identity) != policy_identity(original):
            raise ValueError("MODEL_IDENTITY_MISMATCH: saved policy semantics differ from original model")
        raw_hash = binding.evidence.get("original_spec_raw_sha256")
        if raw_hash is not None and raw_hash != digest(raw_spec.encode("utf-8")):
            raise ValueError("BINDING_MISMATCH: original spec bytes changed")
