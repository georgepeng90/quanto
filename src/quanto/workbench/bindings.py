"""Immutable binding persistence; callers must establish identity evidence first."""
from __future__ import annotations

import json
from pathlib import Path

from quanto.framework import AssetBinding, canonical_hash
from quanto.workbench.store import WorkbenchStore


class BindingStore:
    def __init__(self, store: WorkbenchStore):
        self.store = store

    def bind_dataset(self, dataset_id: str, evidence_ref: str, key: str) -> AssetBinding:
        from quanto.backtest.dataset_loaders import load_dataset
        from quanto.backtest.dataset_identity import EVIDENCE_FILE, PublishedIdentityEvidence, import_binding
        from quanto.workbench.dataset_objects import verify_dataset_version
        if not isinstance(key, str) or not key or not isinstance(evidence_ref, str) or not evidence_ref:
            raise ValueError("identity evidence reference and idempotency key required")
        scope = "dataset.bind"
        fingerprint = canonical_hash({"dataset_id": dataset_id, "evidence_ref": evidence_ref})
        with self.store.transaction() as db:
            dataset = self.store.get_dataset(dataset_id, db)
            root = verify_dataset_version(dataset)
            is_import = (root / EVIDENCE_FILE).is_file()
            if is_import:
                expected_ref = PublishedIdentityEvidence.model_validate_json((root / EVIDENCE_FILE).read_bytes()).ref
            else:
                source = self.store.dataset_market_source(dataset_id, db)
                if source is None:
                    raise ValueError("IDENTITY_UNRESOLVED: dataset has no registered identity evidence")
                expected_ref = f"market:{source['id']}"
            if evidence_ref != expected_ref:
                raise ValueError("IDENTITY_UNRESOLVED: evidence is not registered to this dataset")
            if db.execute("SELECT 1 FROM dataset_asset_bindings WHERE dataset_version_id=?", (dataset_id,)).fetchone() is not None:
                binding = self.get_asset_binding(dataset_id, db)
            else:
                bundle = load_dataset(root)
                if is_import:
                    binding = import_binding(dataset_id, dataset.content_hash, root, bundle)
                else:
                    from quanto.data import MarketData
                    from quanto.backtest.market_dataset import market_asset_binding
                    job = db.execute("SELECT input_path FROM jobs WHERE id=?", (dataset_id,)).fetchone()
                    market_root = self.store.market_root
                    if job is not None and job["input_path"]:
                        market_root = Path(json.loads(Path(job["input_path"]).read_text(encoding="utf-8"))["input"]["market_root"])
                    with MarketData(market_root, providers=()) as market:
                        ref = market.resolve_snapshot(source["id"])
                        if ref.manifest_digest != source["manifest_digest"]:
                            raise ValueError("BINDING_MISMATCH: registered market snapshot digest differs")
                        binding = market_asset_binding(dataset_id, dataset.content_hash, bundle, market, ref, allow_local=True)
            previous = db.execute("SELECT fingerprint FROM framework_idempotency WHERE scope=? AND key=?", (scope, key)).fetchone()
            if previous is not None and previous[0] != fingerprint:
                raise ValueError("dataset binding idempotency conflict")
            result = self._insert_asset_binding(db, binding)
            if previous is None:
                db.execute("INSERT INTO framework_idempotency VALUES (?,?,?,?)", (scope, key, fingerprint, result.model_dump_json()))
            return result

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
