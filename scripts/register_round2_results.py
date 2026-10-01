"""Publish and verify the second research round in the local workbench."""

import json
from pathlib import Path
import time
from uuid import uuid4

import httpx


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "outputs/strategy-research-20261001-round2"
DOCS = PROJECT / "docs/research/2026-10-01-round2"
URL = "http://127.0.0.1:8767"
OLD_DATASET = "acc8a941-75f8-4e27-ac8b-0594d62a43d1"
NEW_DATASET = "be1ead6f-dcf6-40ab-9dde-2f69caec2db1"
BASE_REVISIONS = {"buy_hold": "5918efc0-458b-4ea9-a74d-a1fc2df41201",
                  "trend_20_20_k3": "69d7d862-a1aa-4bc5-967f-7abb30a1f18f",
                  "guarded_momentum_200_v0.1": "bbf3262d-2623-4e9e-9f72-0eeb430a590d"}


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    state_path = ROOT / "workbench_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"revisions": BASE_REVISIONS.copy(), "runs": []}
    fresh = json.loads((DOCS / "fresh_evidence.json").read_text(encoding="utf-8"))
    selection = json.loads((DOCS / "selection.json").read_text(encoding="utf-8"))
    historical = json.loads((DOCS / "historical_evidence.json").read_text(encoding="utf-8"))
    # All new-period comparisons, plus the leading candidate's historical pages.
    rows = fresh + [json.loads((ROOT / "runs" / f"{r['period']}__{r['candidate']}__cost1" / "summary.json").read_text(encoding="utf-8"))
                    for r in historical if r["candidate"] == selection["selected"][0] and r["period"] in ("full", "recent")]
    with httpx.Client(base_url=URL, timeout=30) as client:
        response = client.get("/api/session")
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["token"]

        def request(method, path, payload=None):
            response = client.request(method, path, json=payload) if payload is not None else client.request(method, path)
            response.raise_for_status()
            return response.json()

        for row in rows:
            name = row["candidate"]
            if name not in state["revisions"]:
                config = row["parameters"]
                klass = {"diversified_trend": "DiversifiedTrend", "cash_buy_hold": "CashBuyHold"}[config["kind"]]
                properties = {key: {"type": "string" if isinstance(value, str) else "integer" if type(value) is int else "number", "default": value}
                              for key, value in config["params"].items()}
                manifest = {"name": "round2_" + name, "entrypoint": "strategy.py:create_strategy",
                            "parameter_schema": {"type": "object", "properties": properties, "additionalProperties": False},
                            "required_data_columns": ["date", "symbol", "close"],
                            "auxiliary_sources": ["research_round2_candidates.py"], "research_data": []}
                files = {"strategy.json": json.dumps(manifest, ensure_ascii=False),
                         "strategy.py": f"from research_round2_candidates import {klass}\n\ndef create_strategy(params):\n    return {klass}(**params)\n",
                         "research_round2_candidates.py": (PROJECT / "scripts/research_round2_candidates.py").read_text(encoding="utf-8")}
                revision = request("POST", "/api/strategies", {"manifest": manifest, "files": files})
                state["revisions"][name] = revision["id"]
                write(state_path, state)
            if any(item["candidate"] == name and item["period"] == row["period"] for item in state["runs"]):
                continue
            dataset = NEW_DATASET if row["period"] == "post_selection" else OLD_DATASET
            experiment = request("POST", "/api/experiments", {"payload": {
                "dataset_version_id": dataset, "strategy_revision_id": state["revisions"][name],
                "strategy_params": row["parameters"]["params"], "backtest_spec": row["spec"], "sample_purpose": "exploration"}})
            key = str(uuid4())
            state["runs"].append({"candidate": name, "period": row["period"], "dataset_version_id": dataset,
                                  "strategy_revision_id": state["revisions"][name], "experiment_id": experiment["id"],
                                  "expected_revision": experiment["revision"], "idempotency_key": key})
            write(state_path, state)
        for item in state["runs"]:
            if "run_id" not in item:
                run = request("POST", "/api/runs", {key: item[key] for key in ("experiment_id", "expected_revision", "idempotency_key")} | {"source": "agent"})
                item["run_id"] = run["run_id"]
                write(state_path, state)
        expected = {(row["candidate"], row["period"]): row for row in rows}
        completed = []
        deadline = time.monotonic() + 900
        while len(completed) < len(state["runs"]):
            if time.monotonic() > deadline:
                raise TimeoutError("Workbench runs still incomplete; saved state supports resuming")
            for item in state["runs"]:
                if item["run_id"] in completed:
                    continue
                run = request("GET", f"/api/runs/{item['run_id']}")
                if run["state"] in ("failed", "cancelled"):
                    raise RuntimeError(f"Workbench run failed: {item['run_id']}")
                if run["state"] != "succeeded":
                    continue
                result = request("GET", f"/api/runs/{item['run_id']}/results")
                row = expected[item["candidate"], item["period"]]
                assert result["metrics"] == row["metrics"], item["run_id"]
                assert result["metadata"]["dataset_version_id"] == item["dataset_version_id"]
                assert result["metadata"]["strategy_revision_id"] == item["strategy_revision_id"]
                assert result["completed_through"] == row["spec"]["end"]
                item.update({"all_metrics_equal": True, "url": URL + "/runs/" + item["run_id"]})
                completed.append(item["run_id"])
                write(state_path, state)
                print(f"Verified {item['candidate']} {item['period']}: {item['run_id']}", flush=True)
            if len(completed) < len(state["runs"]):
                time.sleep(3)
        write(DOCS / "workbench_verified.json", state["runs"])


if __name__ == "__main__":
    main()
