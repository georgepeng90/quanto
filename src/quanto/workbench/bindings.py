"""Immutable binding persistence; callers must establish identity evidence first."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from quanto.framework import (AssetBinding, CapabilityCheck, CapabilityReport, ErrorCode,
                              FrozenBasketRevision, ModelFrameworkBinding, canonical_hash, thaw_json)
from quanto.objects import digest, json_bytes
from quanto.workbench.store import WorkbenchStore, utc_now


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

    def get_model_binding(self, model_id: str, db=None) -> ModelFrameworkBinding:
        if db is None:
            with self.store.transaction() as connection:
                return self.get_model_binding(model_id, connection)
        row = db.execute("SELECT * FROM model_framework_bindings WHERE model_id=?", (model_id,)).fetchone()
        if row is None:
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

    def inspect_model_binding(self, model_id: str) -> CapabilityReport:
        try:
            with self.store.transaction() as db:
                existing = db.execute("SELECT 1 FROM model_framework_bindings WHERE model_id=?", (model_id,)).fetchone()
                if existing is not None:
                    binding = self.get_model_binding(model_id, db)
                    self._audit_source(model_id, db, binding.original_spec_hash,
                                       binding.asset_binding.dataset_content_hash)
            if existing is not None:
                self._verify_recorded_artifacts(binding)
                return self._model_report(binding)
            binding = self._prepare_model_binding(model_id)
            return self._model_report(binding)
        except KeyError:
            return self._model_report(None, "IDENTITY_UNRESOLVED: model or dataset binding does not exist")
        except (ValueError, OSError, TypeError, json.JSONDecodeError) as exc:
            return self._model_report(None, str(exc))

    def bind_model(self, model_id: str, key: str) -> ModelFrameworkBinding:
        if not isinstance(model_id, str) or not model_id or not isinstance(key, str) or not key:
            raise ValueError("model id and idempotency key required")
        scope = "model.bind"
        fingerprint = canonical_hash({"model_id": model_id})
        with self.store.transaction() as db:
            previous = db.execute("SELECT fingerprint FROM framework_idempotency WHERE scope=? AND key=?",
                                  (scope, key)).fetchone()
            if previous is not None and previous["fingerprint"] != fingerprint:
                raise ValueError("model binding idempotency conflict")
            if db.execute("SELECT 1 FROM model_framework_bindings WHERE model_id=?", (model_id,)).fetchone():
                result = self.get_model_binding(model_id, db)
                if previous is None:
                    db.execute("INSERT INTO framework_idempotency VALUES (?,?,?,?)",
                               (scope, key, fingerprint, result.model_dump_json()))
                return result
        binding = self._prepare_model_binding(model_id)
        with self.store.transaction() as db:
            previous = db.execute("SELECT fingerprint FROM framework_idempotency WHERE scope=? AND key=?",
                                  (scope, key)).fetchone()
            if previous is not None and previous["fingerprint"] != fingerprint:
                raise ValueError("model binding idempotency conflict")
            if db.execute("SELECT 1 FROM model_framework_bindings WHERE model_id=?", (model_id,)).fetchone():
                result = self.get_model_binding(model_id, db)
            else:
                self._audit_source(model_id, db, binding.original_spec_hash,
                                   binding.asset_binding.dataset_content_hash)
                self._verify_persisted_artifacts(binding, db)
                from quanto.workbench.baskets import BasketStore
                db.execute("INSERT INTO baskets VALUES (?,?,?,?)",
                           (binding.basket.basket_id, f"Audited model {model_id}", 1,
                            binding.basket.created_at.isoformat()))
                BasketStore._insert(db, binding.basket)
                result = self._insert_model_binding(db, binding)
            if previous is None:
                db.execute("INSERT INTO framework_idempotency VALUES (?,?,?,?)",
                           (scope, key, fingerprint, result.model_dump_json()))
            return result

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

    def _verify_persisted_artifacts(self, binding: ModelFrameworkBinding, db=None) -> None:
        from quanto.investing.contracts import PolicyTrainingSpec

        verified = self._audit_artifacts(binding.model_id,
            PolicyTrainingSpec.model_validate(thaw_json(binding.original_spec)),
            binding.asset_binding.dataset_content_hash,
            next(item for item in binding.asset_binding.table_semantics
                 if item.table == "prices").temporal_rules["price_mode"], db)
        evidence = thaw_json(binding.evidence)
        if (verified["policy_artifacts"] != evidence.get("policy_artifacts")
                or verified["limitations"] != evidence.get("limitations")):
            raise ValueError("BINDING_MISMATCH: audited policy evidence changed before publication")

    def _verify_recorded_artifacts(self, binding: ModelFrameworkBinding) -> None:
        from quanto.learning.artifacts import read_manifest
        from quanto.learning.registry import verify_ref

        audited = thaw_json(binding.evidence).get("policy_artifacts", {})
        if not audited:
            return
        with self.store.transaction() as db:
            rows = {row["id"]: row for row in db.execute(
                "SELECT id,kind,path,content_hash,manifest_json,files_json FROM learning_artifacts WHERE id IN "
                f"({','.join('?' for _ in audited)})", tuple(audited))}
        for version_id, evidence in audited.items():
            row = rows.get(version_id)
            if row is None or row["kind"] != "policy" or row["content_hash"] != evidence["content_hash"]:
                raise ValueError("BINDING_MISMATCH: audited policy registry record changed")
            files = json.loads(row["files_json"])
            if files != evidence["files"]:
                raise ValueError("BINDING_MISMATCH: audited policy file registry changed")
            manifest = read_manifest(verify_ref({"path": row["path"], "files": files}), "policy")
            if manifest["id"] != evidence["manifest_id"] or manifest != json.loads(row["manifest_json"]):
                raise ValueError("BINDING_MISMATCH: audited policy manifest changed")

    @staticmethod
    def _model_report(binding: ModelFrameworkBinding | None, error: str | None = None) -> CapabilityReport:
        code = next((item for item in ErrorCode.__args__ if error and error.startswith(item)), "BINDING_MISMATCH")
        checks = (CapabilityCheck(name="model_binding", status="blocked", field_path="model_id",
                                  code=code, message=error),) if error else (
                    CapabilityCheck(name="model_binding", status="passed"),)
        return CapabilityReport(operation="bind_model", status="blocked" if error else "ready",
                                request_hash=binding.binding_hash if binding else None,
                                dataset_version_id=binding.dataset_version_id if binding else None,
                                input_hashes={"model_binding": binding.binding_hash,
                                              "original_spec": binding.original_spec_hash} if binding else {},
                                checks=checks, assumptions=(),
                                limitations=tuple(binding.evidence.get("limitations", ())) if binding else (),
                                checker_version="model-binding-audit-v1", generated_at=datetime.now(timezone.utc))

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
                                      created_at=utc_now(), selection_note=f"Audited from original model {model_id} ordered symbols")
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
        return ModelFrameworkBinding(model_id=model_id, origin="legacy_audited", original_spec=original_spec,
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
        limitations = []
        for row in rows:
            if row["state"] is None:
                raise ValueError("BINDING_MISMATCH: model session lacks its registered job")
            if row["state"] != "succeeded":
                continue
            if row["kind"] != "policy" or not row["path"] or not row["files_json"]:
                raise ValueError("BINDING_MISMATCH: succeeded model version lacks a policy artifact")
            files = json.loads(row["files_json"])
            if "last.zip" not in files or "policy.zip" not in files:
                raise ValueError("BINDING_MISMATCH: saved version lacks continuation or selected weights")
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
            if (manifest.get("model_id") != model_id or manifest.get("dataset_content_hash") != dataset_hash
                    or tuple(manifest.get("basket", ())) != tuple(spec.backtest.symbols)
                    or manifest.get("price_mode") != price_mode
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
            check_policy_identity(manifest, config)
            if manifest.get("feature_transform") is not None:
                read_frozen_transform(root, manifest["feature_transform"])
            if manifest.get("framework_learning") is not None:
                raise ValueError("MODEL_IDENTITY_MISMATCH: existing framework policy needs its original binding")
            content_ids[row["job_id"]] = manifest["id"]
            artifacts[row["job_id"]] = {"manifest_id": manifest["id"],
                                         "content_hash": row["content_hash"], "files": files}
            if row["input_path"]:
                path = Path(row["input_path"])
                if path.is_file():
                    self._verify_job_input(path, row["input_hash"])
                    artifacts[row["job_id"]]["source_snapshot"] = digest(path.read_bytes())
                else:
                    limitations.append(f"Original source snapshot unavailable for {row['job_id']}")
            else:
                limitations.append(f"Original source snapshot unavailable for {row['job_id']}")
        return {"policy_artifacts": artifacts, "limitations": limitations,
                "audit_kind": "original_object_and_current_semantic_equivalence"}

    @staticmethod
    def _policy_rows(db, model_id: str):
        return db.execute("""SELECT s.job_id,s.base_version_id,j.state,j.enqueue_seq,j.input_path,
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
