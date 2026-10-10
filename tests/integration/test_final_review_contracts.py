import copy
import csv
import hashlib
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from quanto.api.app import create_app
from quanto.backtest.engine import run_backtest
from quanto.reports.backtest_report import build_report, write_backtest_outputs
from quanto.strategies.base import TargetPortfolio
from quanto.workbench.service import WorkbenchService, _typed_row
from quanto.workbench.results import compare_runs
from tests.fixtures.engine_cases import bundle_case, spec_case, ScriptedStrategy
from tests.fixtures.framework_imports import fixture_identity_evidence
from tests.integration.test_snapshot_submission import _ready


@pytest.mark.parametrize("change,field,comparable", [
    ({"metrics": {"cash_rate": .1}}, "evaluation", False),
    ({"metrics": {"annual_sessions": 250}}, "evaluation", False),
    ({"initial_cash": 2000}, "initial_cash", False),
    ({"seed": 9}, "seed", True),
])
def test_effective_frozen_configuration_comparison(tmp_path, change, field, comparable):
    store, draft, _, _ = _ready(tmp_path)
    service = WorkbenchService(store.root)
    revised_spec = {**draft.payload["backtest_spec"], **change}
    if "metrics" in change:
        revised_spec["metrics"] = {**draft.payload["backtest_spec"]["metrics"], **change["metrics"]}
    updated = {**draft.payload, "backtest_spec": revised_spec}
    other = store.save_experiment(None, None, updated)
    runs = [service.runs.submit(item.id, item.revision, key, "web")
            for item, key in ((draft, "base"), (other, "changed"))]
    summaries = []
    for run in runs:
        summaries.append({"state": "succeeded", "metrics": {}, "metric_definitions": {},
                          **service._run_provenance(run)})
    compared = compare_runs(summaries)
    assert field in compared.differences
    assert "backtest_spec" in compared.differences
    assert compared.directly_comparable is comparable


@pytest.mark.parametrize("paused", [False, True])
def test_original_signal_and_approved_target_survive_daily_and_final_output(tmp_path, paused):
    bundle = bundle_case({(3, "A"): (10, 5), (4, "A"): (5, 5), (5, "A"): (5, 5)})
    spec = spec_case(bundle, risk={"max_symbol_weight": .5, **({"drawdown_pause": .1} if paused else {})})
    proposed = TargetPortfolio({"A": 1.0}, {"A": "original entry reason"}, {"score": 42})
    events = []
    result = run_backtest(bundle, ScriptedStrategy({day: proposed for day in range(1, 6)}), spec, emit=events.append, cancelled=lambda: False)
    for row in result.signals.to_dict("records"):
        assert row["weights"] == {"A": 1.0}
        assert row["reasons"] == {"A": "original entry reason"}
        assert row["diagnostics"] == {"score": 42}
        assert row["approved_weights"].get("A", 0) <= .5
    assert events[0].payload["tables"]["signals"][0]["approved_weights"] == {"A": .5}
    if paused:
        assert "DRAWDOWN_PAUSE" in set(result.risk_events.code)
        assert events[-1].payload["tables"]["signals"][0]["diagnostics"] == {"score": 42}
    assert result.orders.iloc[0].quantity == 50
    write_backtest_outputs(tmp_path / "final", result, build_report(result, bundle, spec))
    with (tmp_path / "final/signals.csv").open() as stream:
        rows = [_typed_row(row) for row in csv.DictReader(stream)]
    assert rows[0]["weights"] == {"A": 1.0}
    assert rows[0]["approved_weights"] == {"A": .5}
    assert rows[-1]["diagnostics"] == {"score": 42}


@pytest.mark.parametrize("is_demo", [True, False])
def test_authoritative_demo_flag_without_optional_limitations(tmp_path, is_demo):
    bundle = bundle_case()
    bundle = replace(bundle, manifest=bundle.manifest.model_copy(update={"is_demo": is_demo, "limitations": []}))
    spec = spec_case(bundle)
    result = run_backtest(bundle, ScriptedStrategy({}), spec, lambda _: None, lambda: False)
    report = build_report(result, bundle, spec)
    assert report.metadata["is_demo"] is is_demo
    assert report.metadata["source"] == "fixture"
    write_backtest_outputs(tmp_path / "report", result, report)
    metadata = json.loads((tmp_path / "report/metadata.json").read_text(encoding="utf-8"))
    assert metadata["is_demo"] is is_demo
    html = (tmp_path / "report/report.html").read_text(encoding="utf-8")
    assert ('演示数据：合成绩效，不代表真实市场表现' in html) is is_demo


def test_historical_clone_uses_verified_frozen_inputs_and_records_descendant(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    service = WorkbenchService(store.root)
    original = service.runs.submit(draft.id, draft.revision, "historic", "web")
    original_bytes = original.manifest_path.read_bytes()
    changed = copy.deepcopy(draft.payload)
    changed["sample_purpose"] = "later mutable purpose"
    changed["backtest_spec"]["initial_cash"] = 9999
    store.save_experiment(draft.id, draft.revision, changed)
    client = TestClient(create_app(store.root), base_url="http://127.0.0.1:8765")
    headers = {"Authorization": f"Bearer {client.get('/api/session').json()['token']}"}
    response = client.post(f"/api/runs/{original.run_id}/clone", headers=headers)
    assert response.status_code == 200
    clone = response.json()
    assert clone["id"] != draft.id
    assert clone["payload"]["backtest_spec"] == json.loads(original_bytes)["resolved_configuration"]["request"]["backtest_spec"]
    assert clone["payload"]["sample_purpose"] == "exploration"
    assert clone["payload"]["source_run_id"] == original.run_id
    child = service.submit(clone["id"], clone["revision"], "child", "agent")
    assert child["parent_run_id"] == original.run_id
    assert child["provenance"]["source_run_id"] == original.run_id
    assert child["provenance"]["is_demo"] is json.loads(original_bytes)["dataset_manifest"]["is_demo"]
    assert child["provenance"]["source"] == json.loads(original_bytes)["dataset_manifest"]["source"]
    assert original.manifest_path.read_bytes() == original_bytes
    assert hashlib.sha256(original_bytes).hexdigest() == original.manifest_hash
    assert store.get_experiment(draft.id).payload == changed
    (original.manifest_path.parent / "strategy/helper.py").write_text("tampered")
    assert client.post(f"/api/runs/{original.run_id}/clone", headers=headers).status_code == 422


def test_typed_enum_payload_is_supported_by_strict_backend():
    from quanto.config import validate_parameters
    schema = {"type": "object", "properties": {"count": {"type": "integer", "enum": [1, 2]},
              "enabled": {"type": "boolean", "enum": [False, True]}, "mode": {"type": "string", "enum": ["", "x"]}}}
    value = json.loads('{"count":2,"enabled":true,"mode":""}')
    assert validate_parameters(schema, value) == value
    with pytest.raises(ValueError):
        validate_parameters(schema, {"count": "2", "enabled": "true", "mode": ""})


def test_real_worker_api_and_exports_retain_signal_targets_and_demo_identity(tmp_path):
    import io
    import zipfile
    from tests.integration.test_worker_snapshot import run_until_finished
    store, draft, dataset, original_strategy = _ready(tmp_path)
    from tests.fixtures.backtest_cases import dataset_case
    source = dataset_case(tmp_path / "demo-source")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(is_demo=True, limitations=[])
    manifest_path.write_text(json.dumps(manifest))
    demo = store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))
    declaration = original_strategy.manifest
    strategy = store.register_strategy({"strategy.json": json.dumps(declaration).encode(), "helper.py": b"value=1", "strategy.py": b"from quanto.strategies.base import TargetPortfolio\nclass Strategy:\n    warmup_bars=0\n    def should_evaluate(self, ctx): return True\n    def on_close(self, ctx): return TargetPortfolio({'SZSE:etf:000001':1.0}, {'SZSE:etf:000001':'original'}, {'score':42})\ndef create_strategy(params): return Strategy()\n"}, declaration)
    payload = copy.deepcopy(draft.payload)
    payload.update(dataset_version_id=demo.id, strategy_revision_id=strategy.id)
    payload["backtest_spec"]["risk"]["max_symbol_weight"] = .5
    draft = store.save_experiment(draft.id, draft.revision, payload)
    service = WorkbenchService(store.root)
    run = service.runs.submit(draft.id, draft.revision, "demo-trace", "web")
    assert run_until_finished(store, run.job_id).state == "succeeded"
    client = TestClient(create_app(store.root), base_url="http://127.0.0.1:8765")
    headers = {"Authorization": f"Bearer {client.get('/api/session').json()['token']}"}
    result = client.get(f"/api/runs/{run.run_id}/results", headers=headers).json()
    assert result["metadata"]["is_demo"] is True
    assert result["metadata"]["source"] == manifest["source"]
    signals = client.get(f"/api/runs/{run.run_id}/tables/signals", headers=headers).json()["items"]
    assert signals[0]["weights"] == {"SZSE:etf:000001": 1.0}
    assert signals[0]["approved_weights"] == {"SZSE:etf:000001": .5}
    assert signals[0]["diagnostics"] == {"score": 42}
    logs = client.get(f"/api/runs/{run.run_id}/logs", headers=headers).json()["items"]
    day = next(item for item in logs if item["code"] == "DAY_COMPLETED")
    assert day["payload"]["tables"]["signals"][0]["diagnostics"] == {"score": 42}
    for kind in ("csv", "json"):
        response = client.get(f"/api/runs/{run.run_id}/exports/{kind}", headers=headers)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert json.loads(archive.read("metadata.json"))["is_demo"] is True
            if kind == "csv":
                rows = list(csv.DictReader(io.StringIO(archive.read("signals.csv").decode())))
                assert json.loads(rows[0]["approved_weights"]) == {"SZSE:etf:000001": .5}
                assert json.loads(rows[0]["diagnostics"]) == {"score": 42}
    html = client.get(f"/api/runs/{run.run_id}/exports/html", headers=headers).text
    assert '演示数据：合成绩效，不代表真实市场表现' in html


def test_cli_historical_clone_uses_same_endpoint(monkeypatch, capsys):
    from quanto.cli import main, WorkbenchClient
    calls = []
    monkeypatch.setattr(WorkbenchClient, "post", lambda self, path, body: calls.append((path, body)) or {"id": "new-draft"})
    assert main(["--token", "local", "runs", "clone", "historic"]) == 0
    assert calls == [("/runs/historic/clone", {})]
    assert json.loads(capsys.readouterr().out) == {"id": "new-draft"}


def test_schema_one_historical_results_exports_and_mixed_comparison_remain_readable(tmp_path):
    import io
    import zipfile
    from tests.integration.test_worker_snapshot import ready_run, run_until_finished
    store, run, _ = ready_run(tmp_path)
    assert run_until_finished(store, run.job_id).state == "succeeded"
    root = store.artifact_root / run.job_id / "final"
    # Construct a schema-1 historical artifact in this test's temporary workspace only.
    old_columns = ["date", "intent", "weights", "reasons", "diagnostics"]
    for path in root.glob("*.csv"):
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            columns = [name for name in reader.fieldnames or [] if name != "instrument_id"]
            if path.name == "signals.csv":
                columns = old_columns
            rows = list(reader)
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    for name in ("metadata.json", "metrics.json"):
        value = json.loads((root / name).read_text(encoding="utf-8"))
        value["schema_version"] = "1"
        if name == "metadata.json":
            value["table_columns"] = {table: [column for column in columns if column != "instrument_id"]
                                      for table, columns in value["table_columns"].items()}
            value["table_columns"]["signals"] = old_columns
            value.pop("is_demo", None)
            value.pop("source", None)
            value.pop("framework", None)
            value.pop("execution_provenance", None)
        else:
            value["metric_definitions"]["schema_version"]["value"] = "1"
        (root / name).write_text(json.dumps(value))
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.iterdir() if path.name != "manifest.json"}
    (root / "manifest.json").write_text(json.dumps({"schema_version": "1", "files": hashes}))
    hashes["manifest.json"] = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    receipt_path = root.parent / "control/receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["result"]["hashes"] = hashes
    receipt_path.write_text(json.dumps(receipt))
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    service = WorkbenchService(store.root)
    historical = service.results(run.run_id)
    assert historical["metadata"]["schema_version"] == "1"
    assert "is_demo" not in historical["metadata"]
    assert "approved_weights" not in service.table(run.run_id, "signals").items[0]
    for kind in ("csv", "json"):
        data, _ = service.export(run.run_id, kind)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.read("metadata.json") == before["metadata.json"]
    assert service.export(run.run_id, "html")[0] == before["report.html"]
    fresh = service.runs.submit(run.experiment_id, run.experiment_revision, "fresh-schema", "web")
    assert run_until_finished(store, fresh.job_id).state == "succeeded"
    assert service.results(fresh.run_id)["metadata"]["schema_version"] == "3"
    mixed = service.comparison([run.run_id, fresh.run_id])
    assert [item["is_demo"] for item in mixed["runs"]] == [False, False]
    assert [item["identity_status"] for item in mixed["runs"]] == ["unknown", "proven"]
    assert mixed["directly_comparable"] is False
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before


@pytest.mark.parametrize("version", ["0", "3"])
def test_output_unknown_schema_versions_are_rejected(tmp_path, version):
    from quanto.workbench.worker import validate_backtest_output
    bundle = bundle_case()
    spec = spec_case(bundle)
    result = run_backtest(bundle, ScriptedStrategy({}), spec, lambda _: None, lambda: False)
    root = tmp_path / "output"
    hashes = write_backtest_outputs(root, result, build_report(result, bundle, spec))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = version
    (root / "manifest.json").write_text(json.dumps(manifest))
    hashes["manifest.json"] = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="INVALID_OUTPUT"):
        validate_backtest_output(root, hashes)
