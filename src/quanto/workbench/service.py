from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from collections import OrderedDict
import hashlib
import io
import json
import math
from threading import Lock
import zipfile
from pathlib import Path
import shutil
from uuid import uuid4

from quanto.backtest.dataset_schemas import DatasetManifest
from quanto.backtest.dataset_loaders import load_dataset
from quanto.workbench.models import BatchItem, FrozenRun
from quanto.workbench.results import LogPage, Page, TablePage, compare_runs
from quanto.workbench.result_index import PartialResultIndex, ResultIndex
from quanto.workbench.revisions import json_bytes
from quanto.workbench.runs import RunService
from quanto.workbench.scheduler import Scheduler
from quanto.workbench.store import WorkbenchStore, utc_now
from quanto.workbench.worker import atomic_json, read_partial, validate_job_output


TABLES = {"equity", "orders", "fills", "positions", "signals", "risk_events", "account_events",
          "curves", "monthly_returns", "annual_returns", "matched_trades", "open_lots"}
EXPORTS = {"json", "html", "csv"}
INT_FIELDS = {"quantity", "filled_quantity", "expired_quantity", "lot_size", "settlement_sessions",
              "shares", "sellable_shares", "holding_sessions"}
FLOAT_FIELDS = {"cash", "receivables", "market_value", "equity", "stale_value", "price", "commission",
                "slippage_cost", "weight", "cost_basis", "unrealized_pnl", "realized_pnl", "amount",
                "return", "drawdown", "benchmark_equity", "benchmark_return", "proceeds"}
JSON_FIELDS = {"weights", "reasons", "diagnostics", "approved_weights", "approved_reasons", "approved_diagnostics", "details"}
VALIDATION_CACHE_MAX_ENTRIES = 32


def _typed_row(row: dict) -> dict:
    converted = {}
    for key, value in row.items():
        if value == "" or value is None:
            converted[key] = None
        elif key in JSON_FIELDS and isinstance(value, str):
            converted[key] = json.loads(value)
        elif key in INT_FIELDS and isinstance(value, str):
            converted[key] = int(value)
        elif key in FLOAT_FIELDS and isinstance(value, str):
            number = float(value)
            converted[key] = number if math.isfinite(number) else None
        elif key == "full_exit" and isinstance(value, str):
            converted[key] = value.lower() == "true"
        else:
            converted[key] = value
    return converted


def _reverse_complete_lines(path: Path):
    with path.open("rb") as stream:
        stream.seek(0, 2)
        position = stream.tell()
        if not position:
            return
        stream.seek(position - 1)
        drop_last = stream.read(1) != b"\n"
        pending = b""
        while position:
            size = min(position, 8192)
            position -= size
            stream.seek(position)
            parts = (stream.read(size) + pending).split(b"\n")
            pending = parts[0]
            for line in reversed(parts[1:]):
                if not line:
                    continue
                if drop_last:
                    drop_last = False
                    continue
                yield line
        if pending and not drop_last:
            yield pending


class WorkbenchService:
    def __init__(self, root: Path):
        self.store = WorkbenchStore(root)
        self.runs = RunService(self.store)
        self.scheduler = Scheduler(self.store)
        self._cache_lock = Lock()
        self._validated_outputs = OrderedDict()

    @staticmethod
    def _verify_output_hashes(directory: Path, hashes: dict[str, str]) -> None:
        if {path.name for path in directory.iterdir()} != set(hashes):
            raise ValueError("INVALID_OUTPUT: artifact file set changed")
        for name, expected in hashes.items():
            with (directory / name).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError("INVALID_OUTPUT: artifact hash mismatch")

    @staticmethod
    def _record(value):
        def normalize(item):
            if isinstance(item, Path):
                return str(item)
            if isinstance(item, dict):
                return {key: normalize(v) for key, v in item.items()}
            if isinstance(item, (tuple, list)):
                return [normalize(v) for v in item]
            return item
        result = normalize(asdict(value))
        for name in ("path", "manifest_path", "artifact_root", "input_path", "pid", "process_start"):
            result.pop(name, None)
        return result

    def list_resource(self, name: str, cursor: str | None = None, limit: int = 50) -> Page:
        tables = {"datasets": "datasets", "strategies": "strategies", "experiments": "experiments", "runs": "runs", "jobs": "jobs"}
        if name not in tables or not 1 <= limit <= 100:
            raise ValueError("invalid resource or limit")
        offset = int(cursor or "0")
        if offset < 0:
            raise ValueError("invalid cursor")
        with self.store.transaction() as db:
            key = "enqueue_seq" if name == "jobs" else "rowid"
            rows = db.execute(f"SELECT * FROM {tables[name]} ORDER BY {key} LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
        ids = [row["run_id"] if name == "runs" else row["id"] for row in rows[:limit]]
        getter = {"datasets": self.store.get_dataset, "strategies": self.store.get_strategy,
                  "experiments": self.store.get_experiment, "runs": self.store.get_run,
                  "jobs": self.store.get_job}[name]
        items = [self._record(getter(identity)) for identity in ids]
        if name == "runs":
            for item in items:
                item["detail_url"] = f"/runs/{item['run_id']}"
        return Page(items, str(offset + limit) if len(rows) > limit else None)

    def get_resource(self, name: str, identity: str) -> dict:
        getter = {"datasets": self.store.get_dataset, "strategies": self.store.get_strategy,
                  "experiments": self.store.get_experiment, "runs": self.store.get_run,
                  "jobs": self.store.get_job}[name]
        result = self._record(getter(identity))
        if name == "runs":
            result["detail_url"] = f"/runs/{identity}"
            result["provenance"] = self._run_provenance(self.store.get_run(identity))
        if name == "strategies":
            strategy = self.store.get_strategy(identity)
            files = ["strategy.json", strategy.manifest["entrypoint"].partition(":")[0],
                     *strategy.manifest.get("auxiliary_sources", [])]
            result["files"] = {name: (strategy.path / name).read_text(encoding="utf-8") for name in files}
        if name == "jobs":
            receipt = self._job_root(identity) / "control" / "receipt.json"
            if receipt.exists() and result["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                value = json.loads(receipt.read_text(encoding="utf-8"))
                job_result = value.get("result")
                if job_result is not None:
                    result["result"] = {key: item for key, item in job_result.items() if key != "hashes"}
                    if result["kind"] in {"demo", "fetch", "import"} and job_result.get("state") == "complete" and result["state"] == "succeeded":
                        result["result"]["dataset_version_id"] = identity
        return result

    def dataset_detail(self, identity: str) -> dict:
        from quanto.backtest.dataset_identity import (EVIDENCE_FILE, PublishedIdentityEvidence,
                                                     declared_dataset_files, file_digests)
        from quanto.workbench.bindings import BindingStore
        from quanto.workbench.dataset_objects import verify_dataset_version

        dataset = self.store.get_dataset(identity)
        detail = self._record(dataset)
        root = verify_dataset_version(dataset)
        instruments = load_dataset(root).instruments
        detail["instruments"] = [
            {"symbol": str(row.symbol), "lot_size": int(row.lot_size),
             "settlement_sessions": int(row.settlement_sessions),
             "listing_date": row.listing_date.date().isoformat() if hasattr(row.listing_date, "date") else str(row.listing_date),
             "group": str(row.group)}
            for row in instruments.itertuples(index=False)
        ]
        evidence_ref = None
        issue = None
        published_path = root / EVIDENCE_FILE
        if published_path.is_file() and not published_path.is_symlink():
            try:
                published = PublishedIdentityEvidence.model_validate_json(published_path.read_bytes())
                if dict(published.output_files) != file_digests(declared_dataset_files(root)):
                    raise ValueError("published import output hashes differ from dataset")
                evidence_ref = published.ref
            except ValueError as exc:
                issue = {"code": "BINDING_MISMATCH", "message": str(exc)}
        else:
            source = self.store.dataset_market_source(identity)
            if source is not None:
                try:
                    from quanto.data import MarketData, MarketDataError

                    market_root = self.store.market_root
                    try:
                        job = self.store.get_job(identity)
                    except KeyError:
                        job = None
                    if job is not None and job.input_path:
                        market_root = Path(json.loads(Path(job.input_path).read_text(encoding="utf-8"))["input"]["market_root"])
                    with MarketData(market_root, providers=()) as market:
                        verified = market.resolve_snapshot(source["id"])
                    if verified.manifest_digest != source["manifest_digest"]:
                        raise ValueError("registered market snapshot digest differs")
                    evidence_ref = f"market:{source['id']}"
                except (KeyError, OSError, ValueError, MarketDataError) as exc:
                    issue = {"code": "BINDING_MISMATCH", "message": str(exc)}
            else:
                issue = {"code": "IDENTITY_UNRESOLVED", "message": "Dataset has no published identity evidence or registered market source"}
        try:
            binding = BindingStore(self.store).get_asset_binding(identity)
        except KeyError:
            binding = None
            issue = issue or {"code": "IDENTITY_UNRESOLVED", "message": "Identity evidence is available, but no asset binding is registered"}
        except ValueError as exc:
            binding = None
            issue = {"code": "BINDING_MISMATCH", "message": str(exc)}
        detail["asset_binding"] = binding.model_dump(mode="json") if binding is not None else None
        detail["identity_evidence_ref"] = evidence_ref
        detail["identity_binding_issue"] = issue
        return detail

    @staticmethod
    def _run_provenance(run) -> dict:
        manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
        spec = manifest["backtest_spec"]
        environment = manifest["environment"]
        engine_prefixes = ("src/quanto/backtest/", "src/quanto/risk/", "src/quanto/factors/",
                           "src/quanto/data/", "src/quanto/reports/", "src/quanto/strategies/")
        engine_names = {"src/quanto/config.py", "src/quanto/universe.py", "src/quanto/workbench/worker.py"}
        return {"dataset_version_id": manifest["dataset_version_id"],
                "strategy_revision_id": manifest["strategy_revision_id"],
                "strategy_name": manifest["strategy_manifest"].get("name") or manifest["strategy_manifest"]["entrypoint"],
                "strategy_source_hashes": {name: value for name, value in manifest["files"].items() if name.startswith("strategy/")},
                "engine_source_hashes": {name: value for name, value in manifest["files"].items()
                                         if name.startswith(engine_prefixes) or name in engine_names},
                "effective_strategy_params": manifest["effective_strategy_params"],
                "backtest_spec": spec, "symbols": spec["symbols"], "evaluation": spec["metrics"],
                "is_demo": manifest["dataset_manifest"]["is_demo"], "source": manifest["dataset_manifest"]["source"],
                "currency": manifest["dataset_manifest"]["currency"],
                "source_run_id": run.parent_run_id,
                "benchmark": manifest["dataset_manifest"]["benchmark_type"],
                "price_mode": manifest["dataset_manifest"]["price_mode"],
                "start": spec["start"], "end": spec["end"], "costs": spec["execution"],
                "risk": spec["risk"], "initial_cash": spec["initial_cash"], "seed": spec["seed"],
                "metric_definition_version": manifest["metric_definition_version"],
                "execution_definition_version": manifest["execution_definition_version"],
                "risk_definition_version": manifest["risk_definition_version"],
                "environment_fingerprint": hashlib.sha256(json_bytes(environment)).hexdigest(),
                "environment": {key: environment[key] for key in ("python", "platform", "declared_requirements",
                    "declared_optional_requirements", "installed_distributions", "lock_hashes") if key in environment},
                "reproducibility": manifest["reproducibility"], "sample_purpose": manifest["sample_purpose"]}

    def clone_run(self, identity: str) -> dict:
        run = self.store.get_run(identity)
        manifest = self.runs.snapshots.verify(FrozenRun(identity, run.manifest_path, run.manifest_hash), exact=False)
        if manifest.get("schema_version") != 3 or "resolved_configuration" not in manifest:
            raise ValueError("historical run lacks a framework contract; upgrade it to a new draft")
        from quanto.workbench.framework_requests import FixedRequest

        payload = FixedRequest.model_validate({**manifest["resolved_configuration"]["request"],
                                               "source_run_id": identity}).model_dump(mode="json")
        return self._record(self.store.save_experiment(None, None, payload))

    def submit(self, experiment_id: str, expected_revision: int, key: str, source: str = "web") -> dict:
        return self.get_resource("runs", self.runs.submit(experiment_id, expected_revision, key, source).run_id)

    def submit_batch(self, items: list[dict], key: str, source: str = "web") -> list[dict]:
        return [self.get_resource("runs", run.run_id) for run in self.runs.submit_batch([BatchItem(**item) for item in items], key, source)]

    def cancel_run(self, identity: str) -> dict:
        return self.get_resource("runs", self.runs.cancel(identity).run_id)

    def rerun(self, identity: str, key: str, source: str = "web") -> dict:
        return self.get_resource("runs", self.runs.rerun(identity, key, source).run_id)

    def _job_root(self, job_id: str) -> Path:
        job = self.store.get_job(job_id)
        return job.artifact_root / job.id

    def logs(self, job_id: str, cursor: str | None, limit: int = 100, tail: bool = False) -> LogPage:
        job = self.store.get_job(job_id)
        if not 1 <= limit <= 500:
            raise ValueError("invalid limit")
        offset = int(cursor or "0")
        if offset < 0:
            raise ValueError("invalid cursor")
        path = self._job_root(job_id) / "control" / "events.ndjson"
        rows = []
        if tail and path.exists():
            for line in _reverse_complete_lines(path):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("seq", 0) <= offset:
                    break
                payload = item.get("payload")
                if isinstance(payload, dict) and isinstance(payload.get("tables"), dict):
                    tables = payload["tables"]
                    item["payload"] = {key: value for key, value in payload.items() if key != "tables"}
                    item["payload"]["table_counts"] = {name: len(value) for name, value in tables.items()}
                rows.append(item)
                if len(rows) == limit:
                    break
            rows.reverse()
        elif path.exists():
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        break
                    if item.get("seq", 0) > offset:
                        rows.append(item)
                        if len(rows) > limit:
                            break
        items = rows[:limit]
        return LogPage(items, str(items[-1]["seq"]) if items else cursor, job.state, job.completed_through)

    def _verified_final(self, run_id: str, indexed: bool = False) -> tuple[Path, dict, dict]:
        run = self.store.get_run(run_id)
        if run.state != "succeeded":
            raise ValueError("run has no completed results")
        root = self._job_root(run.job_id)
        receipt = json.loads((root / "control" / "receipt.json").read_text(encoding="utf-8"))
        hashes = receipt["result"]["hashes"]
        key = tuple(sorted(hashes.items()))
        if indexed:
            self._verify_output_hashes(root / "final", hashes)
        else:
            cached = False
            with self._cache_lock:
                if key not in self._validated_outputs:
                    validate_job_output("backtest", root, receipt)
                    self._validated_outputs[key] = None
                    if len(self._validated_outputs) > VALIDATION_CACHE_MAX_ENTRIES:
                        self._validated_outputs.popitem(last=False)
                else:
                    self._validated_outputs.move_to_end(key)
                    cached = True
            if cached:
                self._verify_output_hashes(root / "final", hashes)
        return root / "final", json.loads((root / "final" / "metadata.json").read_text(encoding="utf-8")), hashes

    def results(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if run.state != "succeeded":
            control = self._job_root(run.job_id) / "control"
            partial = read_partial(control)
            return {"run_id": run_id, "state": run.state, "completed_through": partial["completed_through"],
                    "metrics": None, "metric_definitions": {}, "held_symbols": None,
                    "tables": partial["tables"], "error": run.error,
                    "detail_url": f"/runs/{run_id}"}
        root, metadata, hashes = self._verified_final(run_id, indexed=True)
        index = ResultIndex(self.store.root, run_id, hashes)
        receipt = json.loads((root.parent / "control" / "receipt.json").read_text(encoding="utf-8"))
        index.ensure(root, lambda: validate_job_output("backtest", root.parent, receipt))
        metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
        with self.store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS result_views (run_id TEXT, viewed_at TEXT, interval_start TEXT, interval_end TEXT)")
            manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
            spec = manifest["backtest_spec"]
            db.execute("INSERT INTO result_views VALUES (?,?,?,?)", (run_id, datetime.now(timezone.utc).isoformat(), spec["start"], spec["end"]))
        return {"run_id": run_id, "state": run.state, "completed_through": run.completed_through,
                "metrics": metrics["metrics"], "metric_definitions": metrics.get("metric_definitions", {}),
                "warnings": metrics.get("warnings", []), "metadata": metadata,
                "held_symbols": index.held_symbols(), "detail_url": f"/runs/{run_id}"}

    @staticmethod
    def _query_dates(date_from: str | None, date_to: str | None, context_date: str | None) -> None:
        for value in (date_from, date_to, context_date):
            if value is not None and (len(value) != 10 or date.fromisoformat(value).isoformat() != value):
                raise ValueError("invalid date")
        if date_from and date_to and date_from > date_to:
            raise ValueError("invalid date range")
        if context_date and (date_from or date_to):
            raise ValueError("context date cannot be combined with range")

    def table(self, run_id: str, name: str, cursor: str | None = None, limit: int = 100, *,
              date_from: str | None = None, date_to: str | None = None, symbol: str | None = None,
              context_date: str | None = None) -> TablePage:
        if name not in TABLES or not 1 <= limit <= 500:
            raise ValueError("invalid table or limit")
        self._query_dates(date_from, date_to, context_date)
        offset = int(cursor or "0")
        if offset < 0:
            raise ValueError("invalid cursor")
        run = self.store.get_run(run_id)
        if run.state == "succeeded":
            root, _, hashes = self._verified_final(run_id, indexed=True)
            index = ResultIndex(self.store.root, run_id, hashes)
            receipt = json.loads((root.parent / "control" / "receipt.json").read_text(encoding="utf-8"))
            index.ensure(root, lambda: validate_job_output("backtest", root.parent, receipt))
            items, total = index.page(name, offset, limit, date_from, date_to, symbol, context_date)
            next_cursor = str(offset + limit) if offset + limit < total else None
        else:
            index = PartialResultIndex(self._job_root(run.job_id) / "control")
            items, total = index.page(name, offset, limit, date_from, date_to, symbol, context_date)
            next_cursor = str(offset + len(items)) if items else None
        return TablePage(items, next_cursor, total)

    def chart(self, run_id: str, name: str, date_from: str | None = None,
              date_to: str | None = None, max_points: int = 1200) -> dict:
        if name not in {"curves", "monthly_returns", "annual_returns", "allocation"} or not 2 <= max_points <= 5000:
            raise ValueError("invalid chart or max_points")
        self._query_dates(date_from, date_to, None)
        root, _, hashes = self._verified_final(run_id, indexed=True)
        index = ResultIndex(self.store.root, run_id, hashes)
        receipt = json.loads((root.parent / "control" / "receipt.json").read_text(encoding="utf-8"))
        index.ensure(root, lambda: validate_job_output("backtest", root.parent, receipt))
        return index.chart(name, date_from, date_to, max_points)

    def export(self, run_id: str, format: str) -> tuple[bytes, str]:
        if format not in EXPORTS:
            raise ValueError("invalid export format")
        root, _, _ = self._verified_final(run_id)
        if format == "html":
            return (root / "report.html").read_bytes(), "html"
        names = ["metrics.json", "metadata.json", "manifest.json"]
        if format == "csv":
            names += sorted(f"{name}.csv" for name in TABLES)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in names:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                archive.writestr(info, (root / name).read_bytes())
        return stream.getvalue(), "zip"

    def comparison(self, run_ids: list[str]) -> dict:
        summaries = []
        for run_id in run_ids:
            run = self.store.get_run(run_id)
            summary = {"run_id": run_id, "state": run.state, **self._run_provenance(run),
                       "metric_definitions": None, "metrics": None}
            if run.state == "succeeded":
                result = self.results(run_id)
                summary["metric_definitions"] = result["metric_definitions"]
                summary["metrics"] = result["metrics"]
            summaries.append(summary)
        return asdict(compare_runs(summaries))

    def upload(self, filename: str, content: bytes) -> str:
        if not content or len(content) > 100_000_000:
            raise ValueError("invalid upload size")
        suffix = Path(filename).suffix.lower()
        if suffix != ".zip":
            raise ValueError("unsupported upload type")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                archive.namelist()
        except zipfile.BadZipFile as exc:
            raise ValueError("invalid ZIP archive") from exc
        identity = str(uuid4())
        path = self.store.root / "uploads" / f"{identity}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return identity

    def preflight_upload(self, upload_id: str) -> dict:
        from uuid import UUID
        UUID(upload_id)
        candidates = list((self.store.root / "uploads").glob(f"{upload_id}.*"))
        if len(candidates) != 1 or candidates[0].suffix != ".zip":
            raise KeyError(upload_id)
        try:
            with zipfile.ZipFile(candidates[0]) as archive:
                if "manifest.json" not in archive.namelist() or archive.getinfo("manifest.json").file_size > 1_000_000:
                    raise ValueError("invalid dataset manifest")
                required = {"prices", "benchmark", "calendar", "instruments", "status", "events"}
                missing = sorted(name for name in required if not any(f"{name}.{suffix}" in archive.namelist()
                                                                       for suffix in ("csv", "parquet")))
                manifest = DatasetManifest.model_validate(json.loads(archive.read("manifest.json")))
        except zipfile.BadZipFile as exc:
            raise ValueError("invalid ZIP archive") from exc
        return {"price_mode": manifest.price_mode, "is_demo": manifest.is_demo,
                "event_coverage": manifest.event_coverage, "calendar_start": manifest.calendar_start.isoformat(),
                "calendar_end": manifest.calendar_end.isoformat(), "data_start": manifest.data_start.isoformat(),
                "data_end": manifest.data_end.isoformat(), "limitations": manifest.limitations,
                "missing_tables": missing, "validation": "metadata_only"}

    def submit_job(self, kind: str, payload: dict) -> dict:
        if kind not in {"demo", "fetch", "import", "validate_strategy"}:
            raise ValueError("invalid job kind")
        if not isinstance(payload, dict) or any(key in payload for key in ("path", "host_path", "source_root", "handler", "workspace", "_output_dir")):
            raise ValueError("host paths and handlers are not accepted")
        fetch_request = fetch_report = None
        if kind == "fetch":
            from quanto.workbench.configuration import FrameworkBlocked, FrameworkConflict, FrameworkResolver
            from quanto.workbench.framework_requests import FetchRequest
            allowed = {*FetchRequest.model_fields, "refresh"}
            if set(payload) - allowed or type(payload.get("refresh", False)) is not bool:
                raise ValueError("fetch accepts data requirements and refresh only")
            fetch_request = FetchRequest.model_validate({key: value for key, value in payload.items() if key != "refresh"})
            fetch_report = FrameworkResolver(self.store).check(fetch_request)
            if fetch_report.status != "ready":
                raise FrameworkBlocked(fetch_report)
            payload = {**fetch_request.model_dump(mode="json"), "refresh": payload.get("refresh", False)}
        if kind == "demo" and (type(payload.get("years", 1)) is not int or not 1 <= payload.get("years", 1) <= 20
                               or type(payload.get("seed", 7)) is not int):
            raise ValueError("invalid demo settings")
        if kind == "validate_strategy" and (not isinstance(payload.get("strategy_revision_id"), str)
                                            or not isinstance(payload.get("params", {}), dict)):
            raise ValueError("invalid strategy validation request")
        identity = str(uuid4())
        input_root = self.store.root / "job-inputs" / identity
        source_root = Path(__file__).resolve().parents[3]
        created = False
        try:
            input_root.mkdir(parents=True, exist_ok=False)
            created = True
            shutil.copytree(source_root / "src" / "quanto", input_root / "src" / "quanto",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            frozen_input = dict(payload)
            frozen_input["workspace"] = str(self.store.root)
            if kind == "fetch":
                frozen_input["market_root"] = str(self.store.market_root)
                frozen_input["framework"] = {"request": fetch_request.model_dump(mode="json"),
                                             "capability_report": fetch_report.model_dump(mode="json")}
            if kind == "import":
                from uuid import UUID
                upload_id = frozen_input.get("upload_id", "")
                UUID(upload_id)
                candidates = list((self.store.root / "uploads").glob(f"{upload_id}.*"))
                if len(candidates) != 1:
                    raise ValueError("unknown upload_id")
                frozen = input_root / f"upload{candidates[0].suffix}"
                shutil.copyfile(candidates[0], frozen)
                frozen_input["_upload_path"] = str(frozen)
            hashes = {path.relative_to(input_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in input_root.rglob("*") if path.is_file()}
            atomic_json(input_root / "input.json", {"source_root": str(input_root), "input": frozen_input,
                                                    "source_hashes": hashes})
            with self.store.transaction() as db:
                if fetch_request is not None:
                    current = FrameworkResolver(self.store).check(fetch_request, db=db)
                    exclude = {"generated_at"}
                    if (current.status != "ready" or
                            current.model_dump(mode="json", exclude=exclude) != fetch_report.model_dump(mode="json", exclude=exclude)):
                        raise FrameworkConflict(current)
                seq = db.execute("SELECT COALESCE(MAX(enqueue_seq),0)+1 FROM jobs").fetchone()[0]
                db.execute("INSERT INTO jobs (id,kind,state,run_id,enqueue_seq,artifact_root,input_path,created_at) VALUES (?,?,'queued',NULL,?,?,?,?)",
                           (identity, kind, seq, str(self.store.artifact_root), str(input_root / "input.json"), utc_now()))
        except BaseException:
            if created:
                if input_root.parent.resolve() != (self.store.root / "job-inputs").resolve():
                    raise ValueError("invalid job input cleanup path")
                shutil.rmtree(input_root)
            raise
        return self.get_resource("jobs", identity)
