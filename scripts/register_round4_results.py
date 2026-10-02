"""Publish the three frozen Round 4 selections for full and late periods."""

from __future__ import annotations

import json
from pathlib import Path
import time
from uuid import uuid4

import httpx

from quanto.workbench.revisions import digest, json_bytes
from scripts.register_round3_results import dataset_hash, list_all, read, sha256, verify_dataset, write


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "outputs/strategy-research-20261002-round4"
DOCS = PROJECT / "docs/research/2026-10-02-round4"
STATE = ROOT / "workbench_state.json"
URL = "http://127.0.0.1:8767"
DATASET_ID = "16dd1e21-836c-453a-95c0-958eef354a56"
SOURCE_NAMES = ("scripts/research_round4_candidates.py", "scripts/research_round3_candidates.py")


def verify_sources(protocol):
    """Check every frozen source, including sources outside the strategy bundle."""
    for name, expected in protocol["source_files"].items():
        path = (PROJECT / name).resolve()
        if not path.is_relative_to(PROJECT) or sha256(path) != expected:
            raise ValueError(f"Frozen source changed: {name}")
    for name in SOURCE_NAMES:
        if name not in protocol["source_files"]:
            raise ValueError(f"Missing frozen strategy source: {name}")


def expected_rows(protocol, selection):
    families = ("hysteresis", "volatility", "combined")
    winners = selection["selected_by_family"]
    names = selection["selected"]
    if (set(winners) != set(families) or len(names) != 3 or len(set(names)) != 3
            or set(names) != set(winners.values())):
        raise ValueError("Expected one distinct selected candidate per new family")
    for family, name in winners.items():
        if (name not in protocol["families"][family]
                or protocol["candidates"][name]["kind"] != "breadth_risk"):
            raise ValueError(f"Selection is outside its frozen family: {name}")
    rows = {}
    for period in ("full", "late"):
        for name in names:
            path = ROOT / "runs" / f"{period}__{name}__cost1" / "summary.json"
            row = read(path)
            if ((row["candidate"], row["period"], row["cost_multiplier"], row["parameters"])
                    != (name, period, 1, protocol["candidates"][name])):
                raise ValueError(f"Frozen result identity mismatch: {path}")
            if row.get("protocol_sha256") != selection["protocol_sha256"]:
                raise ValueError(f"Frozen result protocol mismatch: {path}")
            if not isinstance(row.get("metrics"), dict) or not row["metrics"] or "end" not in row["spec"]:
                raise ValueError(f"Incomplete frozen result: {path}")
            rows[(name, period, 1)] = (row, sha256(path))
    return rows


def strategy_bundle(name, config, round4_source, round3_source):
    if config["kind"] != "breadth_risk":
        raise ValueError(f"Not a Round 4 strategy: {name}")
    params = config["params"]
    properties = {key: {"type": "null" if value is None else "integer" if type(value) is int else "number",
                        "default": value} for key, value in params.items()}
    manifest = {"name": "round4_" + name, "entrypoint": "strategy.py:create_strategy",
                "parameter_schema": {"type": "object", "properties": properties,
                                     "additionalProperties": False},
                "required_data_columns": ["date", "symbol", "close", "amount"],
                "auxiliary_sources": ["scripts/__init__.py", *SOURCE_NAMES], "research_data": []}
    files = {"strategy.json": json.dumps(manifest, ensure_ascii=False),
             "strategy.py": "from scripts.research_round4_candidates import BreadthRiskStrategy\n\n"
                            "def create_strategy(params):\n    return BreadthRiskStrategy(**params)\n",
             "scripts/__init__.py": "",
             SOURCE_NAMES[0]: round4_source, SOURCE_NAMES[1]: round3_source}
    content_hash = digest(json_bytes({key: digest(value.encode("utf-8")) for key, value in files.items()}))
    return manifest, files, content_hash


def ensure_dataset(request, state, protocol):
    expected, normalized_hashes = dataset_hash(protocol)
    if state.get("dataset_content_hash", expected) != expected:
        raise ValueError("Saved dataset hash differs from frozen data")
    detail = request("GET", f"/api/datasets/{DATASET_ID}")
    if detail["content_hash"] != expected:
        raise ValueError("Existing workbench dataset hash differs")
    verification = verify_dataset(DATASET_ID, expected, normalized_hashes)
    state.update(dataset_version_id=DATASET_ID, dataset_content_hash=expected,
                 dataset_verification=verification)
    write(STATE, state)
    write(DOCS / "workbench_dataset_verification.json", verification)
    return DATASET_ID


def ensure_strategy(request, state, name, config, sources):
    manifest, files, expected = strategy_bundle(name, config, *sources)
    revisions = state.setdefault("revisions", {})
    if name in revisions:
        detail = request("GET", f"/api/strategies/{revisions[name]}")
        if detail["content_hash"] != expected:
            raise ValueError(f"Saved strategy revision differs: {name}")
        return revisions[name]
    matches = [item for item in list_all(request, "strategies") if item["content_hash"] == expected]
    if len(matches) > 1:
        raise ValueError(f"Multiple matching strategy revisions: {name}")
    identity = matches[0]["id"] if matches else request(
        "POST", "/api/strategies", json={"manifest": manifest, "files": files})["id"]
    revisions[name] = identity
    write(STATE, state)
    return identity


def experiment_payload(item, row):
    return {"dataset_version_id": item["dataset_version_id"],
            "strategy_revision_id": item["strategy_revision_id"],
            "strategy_params": row["parameters"]["params"],
            "backtest_spec": row["spec"], "sample_purpose": "exploration"}


def ensure_experiment(request, state, item, row):
    payload = experiment_payload(item, row)
    canonical = {**payload, "effective_strategy_params": payload["strategy_params"]}
    if "experiment_id" in item:
        detail = request("GET", f"/api/experiments/{item['experiment_id']}")
        if detail["payload"] != canonical or detail["revision"] != item["expected_revision"]:
            raise ValueError(f"Saved experiment differs: {item['candidate']} {item['period']}")
        return
    # Experiment creation has no idempotency key. Reconcile a lost response by
    # exact immutable payload before another create request.
    matches = [record for record in list_all(request, "experiments") if record["payload"] == canonical]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous matching experiments: {item['candidate']} {item['period']}")
    item["experiment_request_started"] = True
    write(STATE, state)
    experiment = matches[0] if matches else request("POST", "/api/experiments", json={"payload": payload})
    item["experiment_id"] = experiment["id"]
    item["expected_revision"] = experiment["revision"]
    write(STATE, state)


def submit_run(request, state, item, state_path=STATE):
    if item.get("verified"):
        return item["run_id"]
    if item.get("attempts") and item["attempts"][-1].get("state") in {"failed", "cancelled", "interrupted"}:
        raise RuntimeError(f"Failed workbench attempt {item.get('run_id')}: {item['attempts'][-1]}")
    if "run_id" in item:
        return item["run_id"]
    attempt = item.setdefault("attempts", [{"idempotency_key": item["idempotency_key"]}])[-1]
    if attempt["idempotency_key"] != item["idempotency_key"]:
        raise ValueError("Saved submission key differs from attempt")
    write(state_path, state)
    payload = {key: item[key] for key in ("experiment_id", "expected_revision", "idempotency_key")}
    run = request("POST", "/api/runs", json={**payload, "source": "agent"})
    item["run_id"] = run["run_id"]
    attempt["run_id"] = run["run_id"]
    write(state_path, state)
    return run["run_id"]


def verify_run(request, item, row):
    run = request("GET", f"/api/runs/{item['run_id']}")
    attempt = item["attempts"][-1]
    if run["state"] in {"failed", "cancelled", "interrupted"}:
        attempt.update(state=run["state"], job_id=run.get("job_id"),
                       error=run.get("error"), completed_through=run.get("completed_through"))
        return "failed"
    if run["state"] != "succeeded":
        return "pending"
    result = request("GET", f"/api/runs/{item['run_id']}/results")
    if result["metrics"] != row["metrics"]:
        raise ValueError(f"Metric dictionary differs: {item['run_id']}")
    if (result["metadata"]["dataset_version_id"] != item["dataset_version_id"]
            or result["metadata"]["strategy_revision_id"] != item["strategy_revision_id"]
            or result["completed_through"] != row["spec"]["end"]):
        raise ValueError(f"Workbench provenance differs: {item['run_id']}")
    attempt.update(state="succeeded", job_id=run["job_id"],
                   completed_through=run["completed_through"])
    item["verified"] = True
    item["all_metrics_equal"] = True
    item["url"] = URL + "/runs/" + item["run_id"]
    return "verified"


def main():
    protocol_path = ROOT / "protocol.json"
    protocol = read(protocol_path)
    selection = read(ROOT / "selection.json")
    protocol_hash = sha256(protocol_path)
    if selection["protocol_sha256"] != protocol_hash:
        raise ValueError("Selection does not match frozen protocol")
    verify_sources(protocol)
    rows = expected_rows(protocol, selection)
    sources = tuple((PROJECT / name).read_text(encoding="utf-8") for name in SOURCE_NAMES)
    state = read(STATE) if STATE.exists() else {"protocol_sha256": protocol_hash,
                                                "selection_sha256": sha256(ROOT / "selection.json"),
                                                "revisions": {}, "runs": []}
    if (state["protocol_sha256"] != protocol_hash
            or state["selection_sha256"] != sha256(ROOT / "selection.json")):
        raise ValueError("Saved publication state differs from frozen selection")
    saved = {(item["candidate"], item["period"], item["cost_multiplier"]): item
             for item in state["runs"]}
    if len(saved) != len(state["runs"]) or set(saved) - set(rows):
        raise ValueError("Saved runs differ from publication set")
    for key, (row, summary_hash) in rows.items():
        if key in saved and saved[key]["summary_sha256"] != summary_hash:
            raise ValueError(f"Frozen result changed after publication started: {key}")
    write(STATE, state)
    with httpx.Client(base_url=URL, timeout=60) as client:
        response = client.get("/api/session")
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["token"]

        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        dataset_id = ensure_dataset(request, state, protocol)
        for key, (row, summary_hash) in rows.items():
            name, period, cost = key
            revision_id = ensure_strategy(request, state, name, row["parameters"], sources)
            if key in saved:
                item = saved[key]
                if (item["dataset_version_id"], item["strategy_revision_id"]) != (dataset_id, revision_id):
                    raise ValueError(f"Saved workbench identity differs: {key}")
            else:
                item = {"candidate": name, "period": period, "cost_multiplier": cost,
                        "summary_sha256": summary_hash, "dataset_version_id": dataset_id,
                        "strategy_revision_id": revision_id, "idempotency_key": str(uuid4())}
                state["runs"].append(item)
                saved[key] = item
                write(STATE, state)
            ensure_experiment(request, state, item, row)
            submit_run(request, state, item)

        deadline = time.monotonic() + 3600
        while True:
            pending = 0
            for key, (row, _) in rows.items():
                item = saved[key]
                status = verify_run(request, item, row)
                if status != "pending":
                    write(STATE, state)
                if status == "failed":
                    raise RuntimeError(f"Workbench run failed; inspect saved attempt: {key}")
                pending += status == "pending"
            if not pending:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{pending} workbench runs still incomplete; rerun to resume")
            time.sleep(5)
    write(DOCS / "workbench_verified.json", state["runs"])
    print(f"Verified {len(state['runs'])} workbench results: {URL}")


if __name__ == "__main__":
    main()
