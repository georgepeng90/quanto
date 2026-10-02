"""Register frozen Round 5 selections in the local workbench and verify results.

Submit saves receipts and queues runs; check polls submitted runs and writes verified
evidence. Neither phase changes the frozen research selection.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from quanto.workbench.revisions import digest, json_bytes
from scripts.register_round3_results import dataset_hash, list_all, read, sha256, verify_dataset, write
from scripts.register_round4_results import experiment_payload, verify_run


ROOT = PROJECT / "outputs/strategy-research-20261002-round5"
DOCS = PROJECT / "docs/research/2026-10-02-round5"
URL = "http://127.0.0.1:8767"
DATASET_ID = "16dd1e21-836c-453a-95c0-958eef354a56"
BASELINE_RUN_ID = "8e32c5e8-43f1-4d0b-9f9d-0802f7e0672e"
SOURCE_NAMES = ("scripts/research_round4_candidates.py", "scripts/research_round3_candidates.py")
PARAMETERS = frozenset(("entry_breadth", "exit_breadth", "target_vol", "vol_window", "gross_target"))


def strategy_bundle(name, config, round4_source, round3_source):
    if config["kind"] != "breadth_risk" or set(config["params"]) != PARAMETERS:
        raise ValueError(f"Not a complete Round 5 breadth-risk configuration: {name}")
    properties = {key: {"type": "integer" if type(value) is int else "number", "default": value}
                  for key, value in config["params"].items()}
    manifest = {"name": "round5_" + name, "entrypoint": "strategy.py:create_strategy",
                "parameter_schema": {"type": "object", "properties": properties,
                                     "additionalProperties": False},
                "required_data_columns": ["date", "symbol", "close", "amount"],
                "auxiliary_sources": ["scripts/__init__.py", *SOURCE_NAMES], "research_data": []}
    files = {"strategy.json": json.dumps(manifest, ensure_ascii=False),
             "strategy.py": "from scripts.research_round4_candidates import BreadthRiskStrategy\n\n"
                            "def create_strategy(params):\n    return BreadthRiskStrategy(**params)\n",
             "scripts/__init__.py": "", SOURCE_NAMES[0]: round4_source,
             SOURCE_NAMES[1]: round3_source}
    content_hash = digest(json_bytes({key: digest(value.encode("utf-8")) for key, value in files.items()}))
    return manifest, files, content_hash


def expected_rows(root, protocol, selection):
    names = selection["selected"]
    if not isinstance(names, list) or len(names) > 3 or len(names) != len(set(names)):
        raise ValueError("Expected at most three distinct selected candidates")
    rows = {}
    for name in names:
        if name == "baseline" or name not in protocol["candidates"]:
            raise ValueError(f"Invalid selected candidate: {name}")
        config = protocol["candidates"][name]
        if config["kind"] != "breadth_risk" or set(config["params"]) != PARAMETERS:
            raise ValueError(f"Incomplete selected candidate: {name}")
        for period in ("full", "late"):
            path = root / "runs" / f"{period}__{name}__cost1" / "summary.json"
            row = read(path)
            if (row.get("candidate"), row.get("period"), row.get("cost_multiplier"), row.get("parameters")) != (
                    name, period, 1, config):
                raise ValueError(f"Frozen result identity mismatch: {path}")
            if row.get("protocol_sha256") != selection["protocol_sha256"]:
                raise ValueError(f"Frozen result protocol mismatch: {path}")
            if (not isinstance(row.get("metrics"), dict) or not row["metrics"]
                    or row.get("spec", {}).get("end") != protocol["periods"][period][1]):
                raise ValueError(f"Incomplete frozen result: {path}")
            rows[(name, period, 1)] = (row, sha256(path))
    return rows


def checked_inputs(root):
    from scripts import research_round3, research_round5

    protocol_path = root / "protocol.json"
    protocol = read(protocol_path)
    research_round3.verify(protocol)
    for name in SOURCE_NAMES:
        if name not in protocol["source_files"]:
            raise ValueError(f"Missing frozen strategy source: {name}")
    selection = research_round5.checked_selection(root, protocol)
    if selection["protocol_sha256"] != sha256(protocol_path):
        raise ValueError("Selection does not match frozen protocol")
    if selection["selected"]:
        research_round3.evidence(root, protocol, ["full", "late"], selection["selected"])
    rows = expected_rows(root, protocol, selection)
    return protocol, selection, rows


def checked_state(root, selection, rows):
    path = root / "workbench_state.json"
    protocol_hash = selection["protocol_sha256"]
    selection_hash = sha256(root / "selection.json")
    state = read(path) if path.exists() else {
        "protocol_sha256": protocol_hash, "selection_sha256": selection_hash,
        "baseline_run_id": BASELINE_RUN_ID, "baseline_url": URL + "/runs/" + BASELINE_RUN_ID,
        "revisions": {}, "runs": []}
    if ((state["protocol_sha256"], state["selection_sha256"], state["baseline_run_id"])
            != (protocol_hash, selection_hash, BASELINE_RUN_ID)):
        raise ValueError("Saved publication state differs from frozen selection")
    saved = {(item["candidate"], item["period"], item["cost_multiplier"]): item for item in state["runs"]}
    if len(saved) != len(state["runs"]) or set(saved) - set(rows):
        raise ValueError("Saved runs differ from publication set")
    for key, item in saved.items():
        if item["summary_sha256"] != rows[key][1]:
            raise ValueError(f"Frozen result changed after publication started: {key}")
    return state, saved, path


def ensure_dataset(request, state, protocol, state_path, docs):
    expected, normalized_hashes = dataset_hash(protocol)
    if state.get("dataset_content_hash", expected) != expected:
        raise ValueError("Saved dataset hash differs from frozen data")
    detail = request("GET", f"/api/datasets/{DATASET_ID}")
    verification = verify_dataset(DATASET_ID, detail["content_hash"], normalized_hashes)
    state.update(dataset_version_id=DATASET_ID, dataset_content_hash=expected,
                 dataset_verification=verification)
    write(state_path, state)
    write(docs / "workbench_dataset_verification.json", verification)
    return DATASET_ID


def ensure_strategy(request, state, name, config, sources, state_path):
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
    write(state_path, state)
    return identity


def ensure_experiment(request, state, item, row, state_path):
    payload = experiment_payload(item, row)
    canonical = {**payload, "effective_strategy_params": payload["strategy_params"]}
    if "experiment_id" in item:
        detail = request("GET", f"/api/experiments/{item['experiment_id']}")
        if detail["payload"] != canonical or detail["revision"] != item["expected_revision"]:
            raise ValueError(f"Saved experiment differs: {item['candidate']} {item['period']}")
        return
    matches = [record for record in list_all(request, "experiments") if record["payload"] == canonical]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous matching experiments: {item['candidate']} {item['period']}")
    item["experiment_request_started"] = True
    write(state_path, state)
    experiment = matches[0] if matches else request("POST", "/api/experiments", json={"payload": payload})
    item["experiment_id"] = experiment["id"]
    item["expected_revision"] = experiment["revision"]
    write(state_path, state)


def submit_run(request, state, item, state_path):
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


def request_session(client):
    response = client.get("/api/session")
    response.raise_for_status()
    client.headers["Authorization"] = "Bearer " + response.json()["token"]

    def request(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    return request


def submit(root, docs, protocol, selection, rows):
    state, saved, state_path = checked_state(root, selection, rows)
    sources = tuple((PROJECT / name).read_text(encoding="utf-8") for name in SOURCE_NAMES)
    with httpx.Client(base_url=URL, timeout=60) as client:
        request = request_session(client)
        dataset_id = ensure_dataset(request, state, protocol, state_path, docs)
        for key, (row, summary_hash) in rows.items():
            name, period, cost = key
            revision_id = ensure_strategy(request, state, name, row["parameters"], sources, state_path)
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
                write(state_path, state)
            ensure_experiment(request, state, item, row, state_path)
            submit_run(request, state, item, state_path)
    return state


def check(root, docs, protocol, selection, rows, timeout=3600):
    state, saved, state_path = checked_state(root, selection, rows)
    if set(saved) != set(rows) or any("run_id" not in item for item in saved.values()):
        raise ValueError("Submission is incomplete; run --phase submit first")
    with httpx.Client(base_url=URL, timeout=60) as client:
        request = request_session(client)
        dataset_id = ensure_dataset(request, state, protocol, state_path, docs)
        if any(item["dataset_version_id"] != dataset_id for item in saved.values()):
            raise ValueError("Saved run dataset differs from canonical dataset")
        sources = tuple((PROJECT / name).read_text(encoding="utf-8") for name in SOURCE_NAMES)
        for key, (row, _) in rows.items():
            detail = request("GET", f"/api/strategies/{saved[key]['strategy_revision_id']}")
            if detail["content_hash"] != strategy_bundle(key[0], row["parameters"], *sources)[2]:
                raise ValueError(f"Saved strategy revision differs: {key}")
        deadline = time.monotonic() + timeout
        while True:
            pending = 0
            for key, (row, _) in rows.items():
                item = saved[key]
                status = verify_run(request, item, row)
                if status != "pending":
                    write(state_path, state)
                if status == "failed":
                    raise RuntimeError(f"Workbench run failed; inspect saved attempt: {key}")
                pending += status == "pending"
            if not pending:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{pending} workbench runs still incomplete; rerun to resume")
            time.sleep(5)
    write(docs / "workbench_verified.json", state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--phase", required=True, choices=("submit", "check"))
    args = parser.parse_args()
    root = args.root.resolve()
    protocol, selection, rows = checked_inputs(root)
    docs = DOCS if root == ROOT else root / "workbench_docs"
    state = submit(root, docs, protocol, selection, rows) if args.phase == "submit" else check(
        root, docs, protocol, selection, rows)
    print(f"{args.phase}: {len(state['runs'])} selected workbench runs; baseline {URL}/runs/{BASELINE_RUN_ID}")


if __name__ == "__main__":
    main()
