"""A verified standalone ETF policy can enter the current model registry unchanged."""
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import policy_identity
from quanto.investing.policy_versions import PolicyVersionStore, verified_policy_manifest
from quanto.learning.artifacts import read_manifest, write_manifest
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from quanto.workbench.configuration import FrameworkResolver
from quanto.workbench.etf_model_migration import ModelMigrator
from quanto.workbench.framework_requests import PolicyBacktestRequest, ResumeRequest
from quanto.workbench.store import utc_now
from tests.integration.test_framework_capabilities import _training_case


def test_standalone_selected_weights_migrate_without_artifact_rewrite(tmp_path):
    store, request, _ = _training_case(tmp_path)
    spec = PolicyTrainingSpec.model_validate(FrameworkResolver(store).resolve(request).internal_training_spec)
    model_id, version_id = "model-orphan", "orphan"
    source = tmp_path / "frozen-job-input"
    source.mkdir()
    frozen = {"input": {"request": {"dataset_id": request.dataset_id,
                                      "spec": spec.model_dump(mode="json")}}, "source_hashes": {}}
    input_path = source / "input.json"
    input_path.write_bytes(json_bytes(frozen))
    artifact = tmp_path / "policy"
    artifact.mkdir()
    (artifact / "policy.zip").write_bytes(b"selected-weights")
    semantics = policy_identity(spec)
    semantics.pop("reward_version")
    semantics.pop("reward_parameters")
    manifest = write_manifest(artifact, {"kind": "policy", "version": 1,
        "config": spec.model_dump(mode="json"), "dataset_content_hash": store.get_dataset(request.dataset_id).content_hash,
        "basket": list(spec.backtest.symbols), "cutoff": spec.validation_end.isoformat(),
        "selection_metric": spec.selection_metric, **semantics})
    before = {name: path.read_bytes() for name, path in ((path.name, path) for path in artifact.iterdir())}
    files = hash_files(artifact)
    with store.transaction() as db:
        db.execute("INSERT INTO investing_models VALUES (?,?,?,?)",
                   (model_id, request.dataset_id, json_bytes(spec.model_dump(mode="json")).decode(), utc_now()))
        db.execute("""INSERT INTO jobs (id,kind,state,enqueue_seq,artifact_root,input_path,created_at)
                      VALUES (?,'train_policy','succeeded',1,?,?,?)""",
                   (version_id, str(tmp_path), str(input_path), utc_now()))
        db.execute("INSERT INTO learning_submissions VALUES (?,?)", (version_id, digest(input_path.read_bytes())))
        db.execute("INSERT INTO investing_sessions VALUES (?,?,?)", (version_id, model_id, None))
        db.execute("INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)",
                   (version_id, "policy", str(artifact), digest(json_bytes(files)),
                    json_bytes(manifest).decode(), json_bytes(files).decode(), utc_now()))
    binding = ModelMigrator(store).migrate_model(model_id)
    context = PolicyVersionStore(store).get(version_id)
    assert context["artifact_model_id"] is None
    assert verified_policy_manifest(read_manifest(artifact, "policy"), context)["model_id"] == model_id
    assert context["framework_learning"]["ordered_assets"][0]["local_symbol"] == spec.backtest.symbols[0]
    assert {path.name: path.read_bytes() for path in artifact.iterdir()} == before
    backtest = PolicyBacktestRequest(version_id=version_id, dataset_id=request.dataset_id,
        spec={"start": spec.validation_end, "end": spec.backtest.end,
              "evaluation_purpose": "development_replay"})
    assert FrameworkResolver(store).check(backtest).status == "ready"
    assert FrameworkResolver(store).check(ResumeRequest(model_id=model_id, additional_timesteps=16)).status == "blocked"
    assert binding.evidence["standalone_adoptions"][manifest["id"]]["version_id"] == version_id
