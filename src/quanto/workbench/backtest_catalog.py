"""A single, append-stable view over submitted backtests."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from quanto.learning.service import verify_result_file
from quanto.workbench.revisions import json_bytes


_KINDS = {"backtest": "fixed", "policy_backtest": "policy", "model_backtest": "model"}
_EXECUTION_FIELDS = {"commission_rate", "minimum_commission", "slippage_rate", "max_stale_sessions", "model"}
_RISK_FIELDS = {"max_total_weight", "max_symbol_weight", "group_caps", "stress_scenarios", "pause_action", "recovery_mode"}
_METRIC_FIELDS = {"annual_sessions", "risk_free_rate", "cash_rate", "definition_version"}


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


def _conditions(spec: dict | None, dataset_hash: str | None, price_mode: str | None,
                valuation_frequency: str | None) -> dict:
    spec = spec if isinstance(spec, dict) else {}
    symbols = spec.get("symbols")
    symbols = sorted(symbols) if isinstance(symbols, list) and all(isinstance(item, str) and item for item in symbols) else []
    return {"period": {"start": spec.get("start"), "end": spec.get("end")},
            "dataset_content_hash": dataset_hash, "price_mode": price_mode,
            "symbols": symbols,
            "execution": spec.get("execution"), "risk": spec.get("risk"),
            "initial_cash": spec.get("initial_cash"), "metrics": spec.get("metrics"),
            "valuation_frequency": valuation_frequency}


def _comparison_key(conditions: dict) -> str | None:
    period = conditions["period"]
    execution, risk, metrics = (conditions[name] for name in ("execution", "risk", "metrics"))
    if (not all(isinstance(period.get(name), str) and period[name] for name in ("start", "end"))
            or not all(isinstance(conditions.get(name), str) and conditions[name]
                       for name in ("dataset_content_hash", "price_mode", "valuation_frequency"))
            or not conditions["symbols"]
            or not isinstance(execution, dict) or not _EXECUTION_FIELDS <= execution.keys()
            or not isinstance(risk, dict) or not _RISK_FIELDS <= risk.keys()
            or not isinstance(metrics, dict) or not _METRIC_FIELDS <= metrics.keys()
            or type(conditions["initial_cash"]) not in (int, float)):
        return None
    if any(execution[name] is None for name in _EXECUTION_FIELDS) or any(risk[name] is None for name in _RISK_FIELDS):
        return None
    if any(metrics[name] is None for name in _METRIC_FIELDS):
        return None
    return hashlib.sha256(json_bytes(conditions)).hexdigest()


def _fixed_input(row) -> tuple[dict, str, str, list[str], str]:
    path = Path(row["manifest_path"])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row["manifest_hash"]:
        raise ValueError("frozen manifest changed")
    manifest = json.loads(raw)
    spec = manifest["backtest_spec"]
    conditions = _conditions(spec, manifest.get("dataset_content_hash"),
                             manifest.get("dataset_manifest", {}).get("price_mode"), "daily")
    name = manifest.get("strategy_manifest", {}).get("name") or "固定策略"
    return conditions, str(name), str(name), spec.get("symbols", []), ""


def _learning_input(store, row) -> tuple[dict, str, str, list[str], str]:
    path = Path(row["input_path"])
    raw = path.read_bytes()
    with store.transaction() as db:
        submission = db.execute("SELECT input_hash FROM learning_submissions WHERE job_id=?", (row["id"],)).fetchone()
    if submission is None or hashlib.sha256(raw).hexdigest() != submission[0]:
        raise ValueError("frozen request changed")
    payload = json.loads(raw)["input"]
    request = payload["request"]
    dataset_hash = payload["dataset_ref"]["content_hash"]
    if row["kind"] == "policy_backtest":
        policy_ref = payload["policy_ref"]
        with store.transaction() as db:
            policy = db.execute("SELECT content_hash,manifest_json FROM learning_artifacts WHERE id=? AND kind='policy'",
                                (policy_ref["id"],)).fetchone()
            dataset = db.execute("SELECT content_hash,manifest_json FROM datasets WHERE id=?",
                                 (request["dataset_id"],)).fetchone()
        if policy is None or policy["content_hash"] != policy_ref["content_hash"]:
            raise ValueError("policy metadata missing or changed")
        training = json.loads(policy["manifest_json"])["config"]
        spec = dict(training["backtest"])
        backtest = request["spec"]
        spec.update(start=backtest["start"], end=backtest["end"])
        spec["execution"] = dict(spec["execution"])
        for key in ("commission_rate", "minimum_commission", "slippage_rate"):
            spec["execution"][key] *= backtest.get("cost_multiplier", 1)
        price_mode = (json.loads(dataset["manifest_json"]).get("price_mode")
                      if dataset is not None and dataset["content_hash"] == dataset_hash else None)
        conditions = _conditions(spec, dataset_hash, price_mode, "daily")
        baseline = backtest.get("baseline", "policy")
        name = str(training.get("name") or "PPO") if baseline == "policy" else {
            "cash": "现金基准", "equal_weight": "等权基准", "buy_hold": "买入持有基准"}.get(baseline, baseline)
        method = "PPO" if baseline == "policy" else name
        return conditions, name, method, spec.get("symbols", []), baseline
    spec = request["spec"]
    with store.transaction() as db:
        dataset = db.execute("SELECT content_hash,manifest_json FROM learning_artifacts WHERE id=? AND kind='dataset'",
                             (request["dataset_id"],)).fetchone()
    manifest = (json.loads(dataset["manifest_json"])
                if dataset is not None and dataset["content_hash"] == dataset_hash else {})
    symbols = manifest.get("instruments", [])
    if not isinstance(symbols, list) or not all(isinstance(item, str) for item in symbols):
        symbols = []
    execution = {"commission_rate": spec.get("commission_rate"), "minimum_commission": spec.get("minimum_commission"),
                 "slippage_rate": spec.get("slippage_rate"), "max_stale_sessions": None, "model": "next_open"}
    risk = {"max_total_weight": spec.get("max_total_weight"), "max_symbol_weight": spec.get("max_symbol_weight"),
            "group_caps": {}, "stress_scenarios": [], "pause_action": "drawdown_pause", "recovery_mode": spec.get("drawdown_pause")}
    metrics = {"annual_sessions": None, "risk_free_rate": None, "cash_rate": spec.get("cash_rate"),
               "definition_version": "intraday_v1"}
    frequency = manifest.get("frequency_minutes")
    conditions = _conditions({**spec, "symbols": symbols, "execution": execution, "risk": risk, "metrics": metrics}, dataset_hash,
                             "research", f"{frequency}m" if frequency else None)
    baseline = spec.get("baseline", "model")
    name = "模型回测" if baseline == "model" else {"equal_weight": "等权基准", "momentum": "动量基准",
                                                  "cash": "现金基准"}.get(baseline, baseline)
    return conditions, name, "模型" if baseline == "model" else name, symbols, baseline


def _fixed_metrics(workbench, row) -> dict:
    final, _, _ = workbench._verified_final(row["run_id"], indexed=True)
    return _saved_metrics(_json_file(final / "metrics.json")["metrics"])


def _learning_summary(row) -> dict:
    if row["result_path"] is None:
        raise ValueError("published result missing")
    files = json.loads(row["files_json"])
    root = Path(row["result_path"])
    for name in ("summary.json", "results.sqlite3"):
        verify_result_file(root / name, files[name])
    summary = _json_file(root / "summary.json")
    if summary != json.loads(row["manifest_json"]):
        raise ValueError("saved summary differs from published summary")
    _saved_metrics(summary.get("metrics"))
    return summary


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
        rows = db.execute("""SELECT jobs.*, runs.manifest_path, runs.manifest_hash, runs.group_id,
                             learning_artifacts.path AS result_path, learning_artifacts.files_json,
                             learning_artifacts.manifest_json
                             FROM jobs LEFT JOIN runs ON runs.job_id=jobs.id
                             LEFT JOIN learning_artifacts ON learning_artifacts.id=jobs.id
                             WHERE jobs.kind IN ('backtest','policy_backtest','model_backtest')
                               AND jobs.enqueue_seq>? ORDER BY jobs.enqueue_seq LIMIT ?""", (after, limit + 1)).fetchall()
    items = []
    for row in rows[:limit]:
        kind = _KINDS[row["kind"]]
        conditions = _conditions(None, None, None, None)
        name, method, symbols = "回测", "", []
        input_error = None
        try:
            if kind == "fixed":
                conditions, name, method, symbols, _ = _fixed_input(row)
            else:
                conditions, name, method, symbols, _ = _learning_input(store, row)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            input_error = f"Frozen input unavailable or invalid: {exc}"
        result_error = None
        metrics = None
        if row["state"] == "succeeded":
            try:
                if kind == "fixed":
                    metrics = _fixed_metrics(workbench, row)
                else:
                    summary = _learning_summary(row)
                    metrics = summary["metrics"]
                    if kind == "policy":
                        saved_spec = summary.get("metadata", {}).get("backtest")
                        if isinstance(saved_spec, dict):
                            conditions = _conditions(saved_spec, summary.get("dataset_content_hash"),
                                                     summary.get("assumptions", {}).get("price_mode"), "daily")
                            symbols = saved_spec.get("symbols", [])
                        baseline = summary.get("baseline")
                        if baseline == "policy":
                            name = summary.get("metadata", {}).get("config", {}).get("name") or name
                            method = "PPO"
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                result_error = f"Saved result unavailable or invalid: {exc}"
        identity = row["run_id"] if kind == "fixed" else row["id"]
        detail_url = (f"/runs/{identity}" if kind == "fixed" else
                      f"/investing?backtest={identity}" if kind == "policy" else f"/learning?backtest={identity}")
        items.append({"id": identity, "job_id": row["id"], "kind": kind, "name": name, "method": method,
                      "state": row["state"], "created_at": row["created_at"], "finished_at": row["finished_at"],
                      "error": row["error"] or input_error, "result_error": result_error,
                      "start": conditions["period"]["start"], "end": conditions["period"]["end"],
                      "symbols": symbols, "metrics": metrics, "conditions": conditions,
                      "comparison_key": _comparison_key(conditions), "detail_url": detail_url,
                      "group_id": row["group_id"] if kind == "fixed" else None})
    return {"items": items, "next_cursor": str(rows[limit - 1]["enqueue_seq"]) if len(rows) > limit else None}
