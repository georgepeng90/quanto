"""Immutable binding persistence; callers must establish identity evidence first."""
from __future__ import annotations

from quanto.framework import AssetBinding
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
