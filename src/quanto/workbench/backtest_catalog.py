"""One catalog over the stored context of submitted backtests."""
from __future__ import annotations

import json
import math
from pathlib import Path

from quanto.learning.service import verify_result_file
from quanto.workbench.run_contexts import ContextStore


_KINDS = {"backtest": "fixed", "policy_backtest": "policy"}


def _json_file(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _saved_metrics(value) -> dict[str, int | float | None]:
    if not isinstance(value, dict) or not value:
        raise ValueError("saved metrics missing")
    if any(item is not None and (type(item) not in (int, float) or not math.isfinite(item)) for item in value.values()):
        raise ValueError("invalid saved metric")
    return value


def _fixed_result(workbench, row) -> dict:
    final, _, _ = workbench._verified_final(row["run_id"], indexed=True)
    return _saved_metrics(_json_file(final / "metrics.json")["metrics"])


def _policy_result(row) -> dict:
    if row["result_path"] is None:
        raise ValueError("published result missing")
    files = json.loads(row["files_json"])
    root = Path(row["result_path"])
    for name in ("summary.json", "results.sqlite3"):
        verify_result_file(root / name, files[name])
    summary = _json_file(root / "summary.json")
    if summary != json.loads(row["manifest_json"]):
        raise ValueError("saved summary differs from published summary")
    return _saved_metrics(summary.get("metrics"))


def catalog(workbench, cursor: str | None = None, limit: int = 50) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid page size")
    try:
        after = int(cursor or "0")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid cursor") from exc
    if after < 0 or str(after) != (cursor or "0"):
        raise ValueError("invalid cursor")
    store = workbench.store
    with store.transaction() as db:
        rows = db.execute("""SELECT jobs.*, runs.group_id,
                             learning_artifacts.path AS result_path, learning_artifacts.files_json,
                             learning_artifacts.manifest_json
                             FROM jobs LEFT JOIN runs ON runs.job_id=jobs.id
                             LEFT JOIN learning_artifacts ON learning_artifacts.id=jobs.id
                             WHERE jobs.kind IN ('backtest','policy_backtest')
                               AND jobs.enqueue_seq>? ORDER BY jobs.enqueue_seq LIMIT ?""", (after, limit + 1)).fetchall()
        contexts = {row["id"]: ContextStore(store).get(row["id"], db) for row in rows[:limit]}
    items = []
    for row in rows[:limit]:
        context = contexts[row["id"]]
        kind = _KINDS[row["kind"]]
        if context["kind"] != kind:
            raise ValueError("backtest context kind differs from job")
        metrics = None
        result_error = None
        if row["state"] == "succeeded":
            try:
                metrics = _fixed_result(workbench, row) if kind == "fixed" else _policy_result(row)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                result_error = f"Saved result unavailable or invalid: {exc}"
        identity = row["run_id"] if kind == "fixed" else row["id"]
        item = {"id": identity, "job_id": row["id"], "kind": kind,
                "name": context["name"], "method": context["method"],
                "state": row["state"], "created_at": row["created_at"], "finished_at": row["finished_at"],
                "error": row["error"], "result_error": result_error,
                "start": context["conditions"]["period"]["start"],
                "end": context["conditions"]["period"]["end"],
                "symbols": context["backtest_spec"]["symbols"], "metrics": metrics,
                "conditions": context["conditions"], "comparison_key": context["comparison_key"],
                "detail_url": f"/runs/{identity}" if kind == "fixed" else f"/investing?backtest={identity}",
                "execution_provenance": context["execution_provenance"],
                "ordered_pairs": context["ordered_pairs"],
                "canonical_assets": context["canonical_assets"],
                "asset_types": context["asset_types"],
                "basket_revision_ref": context["basket_revision_ref"],
                "dataset_source": context["dataset_source"],
                "sample_purpose": context["sample_purpose"],
                "evaluation_purpose": context["evaluation_purpose"],
                "definition_evidence": context["definition_evidence"]}
        if row["group_id"]:
            item["group_id"] = row["group_id"]
        items.append(item)
    return {"items": items, "next_cursor": str(rows[limit - 1]["enqueue_seq"]) if len(rows) > limit else None}
