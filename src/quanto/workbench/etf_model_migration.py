"""One-time migration of historical ETF models into immutable framework bindings."""
from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

from quanto.framework import FrozenBasketRevision, ModelFrameworkBinding, canonical_hash, thaw_json
from quanto.objects import digest, json_bytes
from quanto.workbench.bindings import BindingStore
from quanto.workbench.store import utc_now


class ModelMigrator(BindingStore):
    def migrate_model(self, model_id: str) -> ModelFrameworkBinding:
        """Audit an existing ETF model and publish its current framework binding."""
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model id required")
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM model_framework_bindings WHERE model_id=?", (model_id,)).fetchone():
                binding = self.get_model_binding(model_id, db)
                self._insert_policy_contexts(db, binding)
                return binding
        binding = self._prepare_model_binding(model_id)
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM model_framework_bindings WHERE model_id=?", (model_id,)).fetchone():
                binding = self.get_model_binding(model_id, db)
                self._insert_policy_contexts(db, binding)
                return binding
            self._audit_source(model_id, db, binding.original_spec_hash,
                               binding.asset_binding.dataset_content_hash)
            self._verify_persisted_artifacts(binding, db)
            from quanto.workbench.baskets import BasketStore
            name = str(binding.original_spec.get("name") or "").strip()
            if not name:
                name = f"ETF组合（{len(binding.basket.members)}只）"
            db.execute("INSERT INTO baskets VALUES (?,?,?,?)",
                       (binding.basket.basket_id, name, 1,
                        binding.basket.created_at.isoformat()))
            BasketStore._insert(db, binding.basket)
            binding = self._insert_model_binding(db, binding)
            self._insert_policy_contexts(db, binding)
            return binding

    def _insert_policy_contexts(self, db, binding: ModelFrameworkBinding) -> None:
        from quanto.investing.policy_versions import PolicyVersionStore
        from quanto.learning.artifacts import read_manifest
        from quanto.learning.registry import verify_ref

        versions = PolicyVersionStore(self.store)
        rows = db.execute("""SELECT s.job_id,a.path,a.content_hash,a.files_json,a.manifest_json FROM investing_sessions s
            JOIN jobs j ON j.id=s.job_id JOIN learning_artifacts a ON a.id=s.job_id
            WHERE s.model_id=? AND j.state='succeeded' AND a.kind='policy'""",
                          (binding.model_id,)).fetchall()
        for row in rows:
            files = json.loads(row["files_json"])
            if digest(json_bytes(files)) != row["content_hash"]:
                raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact registry hash differs")
            manifest = read_manifest(verify_ref({"path": row["path"], "files": files}), "policy")
            if manifest != json.loads(row["manifest_json"]):
                raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact registry differs from files")
            context = versions.context_for(binding, manifest, row["job_id"])
            versions._persist_verified_context(db, row["job_id"], context, row["content_hash"])

    def _verify_persisted_artifacts(self, binding: ModelFrameworkBinding, db=None) -> None:
        from quanto.investing.contracts import PolicyTrainingSpec

        verified = self._audit_artifacts(binding.model_id,
            PolicyTrainingSpec.model_validate(thaw_json(binding.original_spec)),
            binding.asset_binding.dataset_content_hash,
            next(item for item in binding.asset_binding.table_semantics
                 if item.table == "prices").temporal_rules["price_mode"], db)
        evidence = thaw_json(binding.evidence)
        if (verified["policy_artifacts"] != evidence.get("policy_artifacts")
                or verified["limitations"] != evidence.get("limitations")
                or verified["standalone_adoptions"] != evidence.get("standalone_adoptions")):
            raise ValueError("BINDING_MISMATCH: audited policy evidence changed before publication")

    def _prepare_model_binding(self, model_id: str) -> ModelFrameworkBinding:
        from quanto.backtest.dataset_loaders import load_dataset
        from quanto.investing.contracts import PolicyTrainingSpec
        from quanto.investing.definitions import etf_learning_definitions
        from quanto.investing.evaluation import policy_identity
        from quanto.investing.framework import model_request_from_spec
        from quanto.investing.legacy_evidence import audited_etf_semantics
        from quanto.backtest.trading import ETF_TRADING_DEFINITION
        from quanto.workbench.configuration import FrameworkResolver
        from quanto.workbench.dataset_objects import verify_dataset_version

        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM investing_models WHERE id=?", (model_id,)).fetchone()
            if row is None:
                raise KeyError(model_id)
            original_spec = json.loads(row["spec_json"])
            original_hash = canonical_hash(original_spec)
            spec = PolicyTrainingSpec.model_validate(original_spec)
            asset = self.get_asset_binding(row["dataset_id"], db)
            self._audit_source(model_id, db, original_hash, asset.dataset_content_hash)
            raw_hash = digest(row["spec_json"].encode("utf-8"))
        by_local = {item.local_symbol: item for item in asset.assets}
        try:
            ordered = tuple(by_local[symbol] for symbol in spec.backtest.symbols)
        except KeyError as exc:
            raise ValueError("IDENTITY_UNRESOLVED: original local symbol is absent from dataset evidence") from exc
        if len({item.instrument_id.key for item in ordered}) != len(ordered):
            raise ValueError("IDENTITY_UNRESOLVED: historical model has duplicate canonical members")
        if any(item.instrument_id.instrument_type != "etf" for item in ordered):
            raise ValueError("DEFINITION_UNAVAILABLE: historical model needs known ETF identity")
        definition = next((item for item in etf_learning_definitions()
                           if (item.action_version, item.reward_version) ==
                           (spec.action_version, spec.reward_version)), None)
        if definition is None:
            raise ValueError("DEFINITION_UNAVAILABLE: unknown historical ETF learning semantics")
        dataset = self.store.get_dataset(asset.dataset_version_id)
        bundle = load_dataset(verify_dataset_version(dataset))
        prices = next((item for item in asset.table_semantics if item.table == "prices"), None)
        if prices is None or bundle.price_mode != prices.temporal_rules.get("price_mode"):
            raise ValueError("BINDING_MISMATCH: dataset price mode differs from identity evidence")
        basket = FrozenBasketRevision(basket_id=f"model-{model_id}", revision=1,
                                      members=tuple(item.instrument_id for item in ordered),
                                      created_at=utc_now(), selection_note=f"Migrated from original model {model_id} ordered symbols")
        request = model_request_from_spec(spec, basket, asset, definition.ref, ETF_TRADING_DEFINITION.ref)
        resolved, report = FrameworkResolver(self.store)._resolve(request, "train", basket_override=basket)
        if resolved is None:
            details = "; ".join(f"{item.field_path}: {item.message}" for item in report.checks if item.status == "blocked")
            code = report.error_codes[0] if report.error_codes else "DATA_REQUIREMENT_UNSATISFIED"
            raise ValueError(f"{code}: {details}")
        evidence = self._audit_artifacts(model_id, spec, asset.dataset_content_hash, bundle.price_mode)
        evidence["original_spec_raw_sha256"] = raw_hash
        evidence["asset_binding_hash"] = asset.binding_hash
        evidence["equivalence_reference"] = audited_etf_semantics(definition, ETF_TRADING_DEFINITION)
        return ModelFrameworkBinding(model_id=model_id, original_spec=original_spec,
                                     training_configuration=resolved, semantic_identity=policy_identity(spec),
                                     evidence=evidence, created_at=utc_now())

    def _audit_source(self, model_id: str, db, original_hash: str, dataset_hash: str) -> None:
        from quanto.workbench.dataset_objects import verify_dataset_version

        row = db.execute("SELECT dataset_id,spec_json FROM investing_models WHERE id=?", (model_id,)).fetchone()
        if row is None or canonical_hash(json.loads(row["spec_json"])) != original_hash:
            raise ValueError("BINDING_MISMATCH: original model spec changed during audit")
        asset = self.get_asset_binding(row["dataset_id"], db)
        if asset.dataset_content_hash != dataset_hash:
            raise ValueError("BINDING_MISMATCH: dataset binding changed during audit")
        verify_dataset_version(self.store.get_dataset(row["dataset_id"], db))

    def _audit_artifacts(self, model_id: str, spec, dataset_hash: str, price_mode: str, db=None) -> dict:
        from quanto.investing.contracts import PolicyTrainingSpec
        from quanto.investing.evaluation import check_policy_identity
        from quanto.learning.artifacts import read_manifest, read_frozen_transform
        from quanto.learning.registry import verify_ref

        if db is None:
            with self.store.transaction() as connection:
                rows = self._policy_rows(connection, model_id)
        else:
            rows = self._policy_rows(db, model_id)
        artifacts: dict[str, dict] = {}
        content_ids: dict[str, str] = {}
        standalone_adoptions = {}
        with self.store.transaction() if db is None else nullcontext(db) as connection:
            source = connection.execute("SELECT dataset_id FROM investing_models WHERE id=?", (model_id,)).fetchone()
        if source is None:
            raise ValueError("BINDING_MISMATCH: model row missing during policy audit")
        for row in rows:
            if row["state"] is None:
                raise ValueError("BINDING_MISMATCH: model session lacks its registered job")
            if row["state"] != "succeeded":
                continue
            if row["kind"] != "policy" or not row["path"] or not row["files_json"]:
                raise ValueError("BINDING_MISMATCH: succeeded model version lacks a policy artifact")
            files = json.loads(row["files_json"])
            if digest(json_bytes(files)) != row["content_hash"]:
                raise ValueError("BINDING_MISMATCH: policy artifact registry hash differs")
            root = verify_ref({"path": row["path"], "files": files})
            manifest = read_manifest(root, "policy")
            if manifest != json.loads(row["manifest_json"]):
                raise ValueError("BINDING_MISMATCH: policy artifact manifest differs from registry")
            if manifest.get("version") != 1:
                raise ValueError("DEFINITION_UNAVAILABLE: policy artifact format is not a known ETF version")
            prior = content_ids.get(row["base_version_id"])
            if row["base_version_id"] and prior is None:
                raise ValueError("BINDING_MISMATCH: parent version is not a prior verified policy")
            adopted = (manifest.get("model_id") is None and
                       self._standalone_source_matches(row, rows, model_id, source["dataset_id"], spec))
            if "policy.zip" not in files or ("last.zip" not in files and not adopted):
                raise ValueError("BINDING_MISMATCH: saved version lacks continuation or selected weights")
            if adopted:
                standalone_adoptions[manifest["id"]] = {"version_id": row["job_id"],
                                                        "input_hash": row["input_hash"]}
            if ((manifest.get("model_id") != model_id and not adopted)
                    or manifest.get("dataset_content_hash") != dataset_hash
                    or tuple(manifest.get("basket", ())) != tuple(spec.backtest.symbols)
                    or (manifest.get("price_mode") != price_mode
                        and not (adopted and manifest.get("price_mode") is None))
                    or manifest.get("parent_content_id") != prior
                    or manifest.get("cutoff") != spec.validation_end.isoformat()
                    or manifest.get("selection_metric") != spec.selection_metric):
                raise ValueError("MODEL_IDENTITY_MISMATCH: saved policy source identity differs")
            source = manifest.get("best_source_content_id")
            if source is not None and source not in content_ids.values():
                raise ValueError("BINDING_MISMATCH: best policy source is not a prior verified version")
            config = PolicyTrainingSpec.model_validate(manifest["config"])
            if (config.model_dump(mode="json", exclude={"total_timesteps", "device"}) !=
                    spec.model_dump(mode="json", exclude={"total_timesteps", "device"})):
                raise ValueError("MODEL_IDENTITY_MISMATCH: saved policy training configuration differs")
            from quanto.investing.rewards import LEGACY_REWARD_VERSION

            historical_semantics = {"reward_version": LEGACY_REWARD_VERSION,
                                    "reward_parameters": {}, **manifest}
            check_policy_identity(historical_semantics, config)
            if manifest.get("feature_transform") is not None:
                read_frozen_transform(root, manifest["feature_transform"])
            if manifest.get("framework_learning") is not None:
                raise ValueError("MODEL_IDENTITY_MISMATCH: existing framework policy needs its original binding")
            content_ids[row["job_id"]] = manifest["id"]
            artifacts[row["job_id"]] = {"manifest_id": manifest["id"],
                                         "content_hash": row["content_hash"], "files": files}
            path = Path(row["input_path"]) if row["input_path"] else None
            if path is None or not path.is_file():
                raise ValueError(f"BINDING_MISMATCH: original source snapshot unavailable for {row['job_id']}")
            self._verify_job_input(path, row["input_hash"])
            artifacts[row["job_id"]]["source_snapshot"] = digest(path.read_bytes())
        return {"policy_artifacts": artifacts, "limitations": [],
                "standalone_adoptions": standalone_adoptions,
                "audit_kind": "original_object_and_current_semantic_equivalence"}

    def _standalone_source_matches(self, row, rows, model_id: str, dataset_id: str, spec) -> bool:
        from quanto.investing.contracts import PolicyTrainingSpec

        if (len(rows) != 1 or row["job_kind"] != "train_policy" or row["base_version_id"] is not None
                or model_id != f"model-{row['job_id']}" or not row["input_path"]):
            return False
        path = Path(row["input_path"])
        if not path.is_file():
            return False
        self._verify_job_input(path, row["input_hash"])
        request = json.loads(path.read_bytes()).get("input", {}).get("request", {})
        if (not isinstance(request, dict) or "model_id" in request
                or request.get("dataset_id") != dataset_id):
            return False
        try:
            frozen_spec = PolicyTrainingSpec.model_validate(request["spec"])
        except (KeyError, ValueError):
            return False
        return frozen_spec == spec

    @staticmethod
    def _policy_rows(db, model_id: str):
        return db.execute("""SELECT s.job_id,s.base_version_id,j.kind AS job_kind,j.state,j.enqueue_seq,j.input_path,
            a.kind,a.path,a.content_hash,a.manifest_json,a.files_json,l.input_hash
            FROM investing_sessions s LEFT JOIN jobs j ON j.id=s.job_id
            LEFT JOIN learning_artifacts a ON a.id=s.job_id
            LEFT JOIN learning_submissions l ON l.job_id=s.job_id
            WHERE s.model_id=? ORDER BY j.enqueue_seq""", (model_id,)).fetchall()

    @staticmethod
    def _verify_job_input(path: Path, expected_hash: str | None) -> None:
        from quanto.learning.registry import hash_files

        raw = path.read_bytes()
        if expected_hash is None or digest(raw) != expected_hash:
            raise ValueError("BINDING_MISMATCH: original job input hash differs")
        payload = json.loads(raw)
        expected = payload.get("source_hashes")
        if not isinstance(expected, dict):
            raise ValueError("BINDING_MISMATCH: original source manifest missing")
        actual = hash_files(path.parent)
        actual.pop(path.name, None)
        if actual != expected:
            raise ValueError("BINDING_MISMATCH: original source snapshot changed")
