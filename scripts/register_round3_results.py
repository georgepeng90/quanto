"""Publish the frozen Round 3 finalists in the local workbench and verify them."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from uuid import uuid4
import zipfile

import httpx
from pandas.testing import assert_frame_equal
import pyarrow.parquet as pq

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.dataset_storage import write_parquet_dataset
from quanto.workbench.revisions import digest, json_bytes, source_files


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "outputs/strategy-research-20261002-round3"
DOCS = PROJECT / "docs/research/2026-10-02-round3"
DATASET = PROJECT / "outputs/etf-expansion-20261002/shared-cache/dataset"
SOURCE = PROJECT / "scripts/research_round3_candidates.py"
URL = "http://127.0.0.1:8767"
STATE = ROOT / "workbench_state.json"
WORKSPACE = PROJECT / "data/research-20261001"
DATA_FILES = ("benchmark.parquet", "calendar.parquet", "events.parquet",
              "instruments.parquet", "manifest.json", "prices.parquet", "status.parquet")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(path.suffix + ".tmp")
    staged.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    staged.replace(path)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def dataset_hash(protocol):
    expected = protocol["data_files"]
    if set(expected) != set(DATA_FILES):
        raise ValueError("Frozen dataset file list differs")
    for name, value in expected.items():
        if sha256(DATASET / name) != value:
            raise ValueError(f"Frozen dataset changed: {name}")
    with tempfile.TemporaryDirectory(prefix="quanto-round3-dataset-") as directory:
        normalized = Path(directory)
        write_parquet_dataset(load_dataset(DATASET), normalized)
        hashes = {name: digest(content) for name, content in source_files(normalized).items()}
    return digest(json_bytes(hashes)), hashes


def verify_dataset(identity, api_hash, normalized_hashes):
    """Prove that the imported canonical data equals the seven frozen source files."""
    with sqlite3.connect(f"file:{WORKSPACE / 'workbench.sqlite3'}?mode=ro", uri=True) as db:
        row = db.execute("SELECT path, content_hash FROM datasets WHERE id=?", (identity,)).fetchone()
    if row is None or row[1] != api_hash:
        raise ValueError("Workbench dataset database identity differs")
    imported = Path(row[0]).resolve()
    if not imported.is_relative_to(WORKSPACE.resolve()):
        raise ValueError("Workbench dataset is outside the expected workspace")
    actual_files = source_files(imported)
    if set(actual_files) - set(DATA_FILES) != {"result.json"}:
        raise ValueError("Imported dataset has unexpected files")
    if digest(json_bytes({name: digest(content) for name, content in actual_files.items()})) != api_hash:
        raise ValueError("Workbench dataset database hash differs from files")
    actual_hashes = {name: digest(actual_files[name]) for name in DATA_FILES}
    if actual_hashes != normalized_hashes:
        raise ValueError("Imported canonical dataset files differ from frozen normalization")
    source, registered = load_dataset(DATASET), load_dataset(imported)
    if source.manifest != registered.manifest:
        raise ValueError("Imported manifest differs from frozen source")
    rows = {}
    for name in ("prices", "benchmark", "calendar", "instruments", "status", "events"):
        assert_frame_equal(getattr(source, name), getattr(registered, name), check_exact=True, check_dtype=True)
        rows[name] = len(getattr(source, name))
        if pq.read_schema(DATASET / f"{name}.parquet") != pq.read_schema(imported / f"{name}.parquet"):
            raise ValueError(f"Imported {name} schema differs")
    return {"dataset_version_id": identity, "workbench_content_hash": api_hash,
            "canonical_file_hashes": actual_hashes, "canonical_rows": rows,
            "manifest_equal": True, "tables_exact": True}


def list_all(request, resource):
    cursor = None
    while True:
        page = request("GET", f"/api/{resource}", params={"limit": 100, **({"cursor": cursor} if cursor else {})})
        yield from page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            return


def wait_job(request, job_id, *, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = request("GET", f"/api/jobs/{job_id}")
        if job["state"] == "succeeded":
            return job
        if job["state"] in {"failed", "cancelled", "interrupted"}:
            raise RuntimeError(f"Dataset import {job_id} ended in {job['state']}: {job.get('error')}")
        time.sleep(3)
    raise TimeoutError(f"Dataset import {job_id} is still running; rerun to resume")


def ensure_dataset(request, state, protocol):
    expected, normalized_hashes = dataset_hash(protocol)
    state.setdefault("dataset_content_hash", expected)
    if state["dataset_content_hash"] != expected:
        raise ValueError("Saved dataset identity differs from frozen snapshot")
    matches = [item for item in list_all(request, "datasets") if item["content_hash"] == expected]
    if state.get("dataset_version_id"):
        detail = request("GET", f"/api/datasets/{state['dataset_version_id']}")
        state["dataset_verification"] = verify_dataset(state["dataset_version_id"], detail["content_hash"], normalized_hashes)
        write(STATE, state)
        return state["dataset_version_id"]
    if matches:
        state["dataset_version_id"] = matches[0]["id"]
        state["dataset_verification"] = verify_dataset(state["dataset_version_id"], matches[0]["content_hash"], normalized_hashes)
        write(STATE, state)
        return state["dataset_version_id"]
    if "import_job_id" not in state:
        if "upload_id" not in state:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for name in DATA_FILES:
                    bundle.write(DATASET / name, arcname=name)
            response = request("POST", "/api/uploads", files={"file": ("round3-dataset.zip", archive.getvalue(), "application/zip")})
            state["upload_id"] = response["upload_id"]
            write(STATE, state)
        preflight = request("POST", "/api/datasets/preflight", json={"upload_id": state["upload_id"]})
        if preflight["missing_tables"]:
            raise ValueError("Dataset preflight found missing tables")
        job = request("POST", "/api/datasets", json={"upload_id": state["upload_id"]})
        state["import_job_id"] = job["id"]
        write(STATE, state)
    job = wait_job(request, state["import_job_id"])
    identity = job["result"]["dataset_version_id"]
    detail = request("GET", f"/api/datasets/{identity}")
    state["dataset_verification"] = verify_dataset(identity, detail["content_hash"], normalized_hashes)
    state["dataset_version_id"] = identity
    write(STATE, state)
    return identity


def strategy_bundle(name, config, source):
    kind = config["kind"]
    klass = {"round3": "Round3Strategy", "group_equal": "GroupEqual"}[kind]
    params = config["params"]
    properties = {key: {"type": "string" if isinstance(value, str) else "integer" if type(value) is int else "number",
                        "default": value} for key, value in params.items()}
    manifest = {"name": "round3_" + name, "entrypoint": "strategy.py:create_strategy",
                "parameter_schema": {"type": "object", "properties": properties, "additionalProperties": False},
                "required_data_columns": ["date", "symbol", "close", "amount"],
                "auxiliary_sources": ["research_round3_candidates.py"], "research_data": []}
    files = {"strategy.json": json.dumps(manifest, ensure_ascii=False),
             "strategy.py": f"from research_round3_candidates import {klass}\n\ndef create_strategy(params):\n    return {klass}(**params)\n",
             "research_round3_candidates.py": source}
    content_hash = digest(json_bytes({key: digest(value.encode("utf-8")) for key, value in files.items()}))
    return manifest, files, content_hash


def ensure_strategy(request, state, name, config, source):
    manifest, files, expected = strategy_bundle(name, config, source)
    revisions = state.setdefault("revisions", {})
    if name in revisions:
        detail = request("GET", f"/api/strategies/{revisions[name]}")
        if detail["content_hash"] != expected:
            raise ValueError(f"Saved strategy revision differs: {name}")
        return revisions[name]
    matches = [item for item in list_all(request, "strategies") if item["content_hash"] == expected]
    if matches:
        identity = matches[0]["id"]
    else:
        identity = request("POST", "/api/strategies", json={"manifest": manifest, "files": files})["id"]
    revisions[name] = identity
    write(STATE, state)
    return identity


def expected_rows(protocol, selection):
    names = selection["selected"] + ["group_equal_80", "group_equal_40"]
    if len(selection["selected"]) != 3 or len(names) != len(set(names)):
        raise ValueError("Expected three distinct finalists plus two group benchmarks")
    rows = []
    for period in ("full", "late"):
        for name in names:
            path = ROOT / "runs" / f"{period}__{name}__cost1" / "summary.json"
            row = read(path)
            if (row["candidate"], row["period"], row["cost_multiplier"], row["parameters"]) != (
                    name, period, 1, protocol["candidates"][name]):
                raise ValueError(f"Frozen result identity mismatch: {path}")
            rows.append(row)
    return rows


def verify_run(request, item, row):
    run = request("GET", f"/api/runs/{item['run_id']}")
    if run["state"] in {"failed", "cancelled", "interrupted"}:
        raise RuntimeError(f"Workbench run {item['run_id']} ended in {run['state']}")
    if run["state"] != "succeeded":
        return False
    result = request("GET", f"/api/runs/{item['run_id']}/results")
    if result["metrics"] != row["metrics"]:
        raise ValueError(f"Metric dictionary differs: {item['run_id']}")
    if (result["metadata"]["dataset_version_id"] != item["dataset_version_id"]
            or result["metadata"]["strategy_revision_id"] != item["strategy_revision_id"]
            or result["completed_through"] != row["spec"]["end"]):
        raise ValueError(f"Workbench provenance differs: {item['run_id']}")
    item["all_metrics_equal"] = True
    item["url"] = URL + "/runs/" + item["run_id"]
    return True


def main():
    protocol = read(ROOT / "protocol.json")
    selection = read(ROOT / "selection.json")
    if selection["protocol_sha256"] != sha256(ROOT / "protocol.json"):
        raise ValueError("Selection does not match frozen protocol")
    if sha256(SOURCE) != protocol["source_files"]["scripts/research_round3_candidates.py"]:
        raise ValueError("Frozen strategy source changed")
    rows = expected_rows(protocol, selection)
    state = read(STATE) if STATE.exists() else {"revisions": {}, "runs": []}
    with httpx.Client(base_url=URL, timeout=60) as client:
        response = client.get("/api/session")
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["token"]

        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        dataset_id = ensure_dataset(request, state, protocol)
        source = SOURCE.read_text(encoding="utf-8")
        expected = {(row["candidate"], row["period"], row["cost_multiplier"]): row for row in rows}
        saved = {(item["candidate"], item["period"], item["cost_multiplier"]): item for item in state["runs"]}
        if len(saved) != len(state["runs"]) or set(saved) - set(expected):
            raise ValueError("Saved workbench runs do not match final publication set")
        for key, row in expected.items():
            name, period, cost = key
            revision_id = ensure_strategy(request, state, name, row["parameters"], source)
            if key in saved:
                item = saved[key]
                if (item["dataset_version_id"], item["strategy_revision_id"]) != (dataset_id, revision_id):
                    raise ValueError(f"Saved workbench identity differs: {key}")
                continue
            experiment = request("POST", "/api/experiments", json={"payload": {
                "dataset_version_id": dataset_id, "strategy_revision_id": revision_id,
                "strategy_params": row["parameters"]["params"], "backtest_spec": row["spec"],
                "sample_purpose": "exploration"}})
            item = {"candidate": name, "period": period, "cost_multiplier": cost,
                    "dataset_version_id": dataset_id, "strategy_revision_id": revision_id,
                    "experiment_id": experiment["id"], "expected_revision": experiment["revision"],
                    "idempotency_key": str(uuid4())}
            state["runs"].append(item)
            saved[key] = item
            write(STATE, state)
        for item in state["runs"]:
            if "run_id" not in item:
                payload = {key: item[key] for key in ("experiment_id", "expected_revision", "idempotency_key")}
                run = request("POST", "/api/runs", json={**payload, "source": "agent"})
                item["run_id"] = run["run_id"]
                write(STATE, state)
        deadline = time.monotonic() + 3600
        while True:
            pending = []
            for item in state["runs"]:
                key = item["candidate"], item["period"], item["cost_multiplier"]
                if verify_run(request, item, expected[key]):
                    write(STATE, state)
                    continue
                pending.append(item)
            if not pending:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{len(pending)} workbench runs still incomplete; rerun to resume")
            time.sleep(5)
    write(DOCS / "workbench_verified.json", state["runs"])
    print(f"Verified {len(state['runs'])} workbench results: {URL}")


if __name__ == "__main__":
    main()
