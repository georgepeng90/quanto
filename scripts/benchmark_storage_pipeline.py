"""Synthetic storage pipeline timings, not an end-to-end strategy benchmark."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import tempfile
import time

import pandas as pd

from quanto.config import BacktestSpec
from quanto.backtest import dataset_loaders as loaders
from quanto.backtest.dataset_identity import make_import_evidence, standard_table_semantics
from quanto.backtest.trading import ETF_TRADING_DEFINITION
from quanto.data.contracts import InstrumentId
from quanto.framework import AssetMapping, BenchmarkBinding
from quanto.workbench.baskets import BasketStore
from quanto.workbench.bindings import BindingStore
from quanto.workbench.configuration import FrameworkResolver
from quanto.workbench.framework_requests import FixedRequest
from quanto.workbench.models import FrozenRun
from quanto.workbench.snapshots import SnapshotBuilder
from quanto.workbench.store import WorkbenchStore


def fixture(root: Path, count: int, days: int) -> tuple[tuple[InstrumentId, ...], InstrumentId]:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.benchmark_market_data import synthetic_case
    from quanto.data import MarketData
    from quanto.data.imports import import_table
    from quanto.backtest.market_dataset import assemble_dataset
    from quanto.backtest.dataset_storage import write_parquet_dataset
    payload, requirement, chunks = synthetic_case(count, days)
    with MarketData(root.parent / "market", providers=()) as market:
        for chunk in chunks:
            import_table(market.store, market.catalog, chunk.request, chunk.frame, chunk.source, chunk.evidence)
        ref = market.wait_ready(market.prefetch(requirement).id)
        bundle = assemble_dataset(market, ref, requirement, {"universe_rule": {"min_history_days": 120, "min_avg_turnover_20d": 0}})
    write_parquet_dataset(bundle, root)
    return (tuple(InstrumentId.model_validate(item) for item in payload["instruments"]),
            InstrumentId.model_validate(payload["benchmark"]))


def timed(operation):
    start = time.perf_counter()
    value = operation()
    return value, time.perf_counter() - start


def size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=100)
    parser.add_argument("--days", type=int, default=2500)
    args = parser.parse_args()
    if args.symbols < 5 or args.days < 250:
        parser.error("symbols >= 5 and days >= 250 required")
    with tempfile.TemporaryDirectory(prefix="quanto-pipeline-bench-") as directory:
        root = Path(directory)
        instruments, benchmark = fixture(root / "input", args.symbols, args.days)
        bundle, load_seconds = timed(lambda: loaders.load_dataset(root / "input"))
        store = WorkbenchStore(root / "workspace")
        evidence_ref = "synthetic:benchmark-storage-pipeline"
        evidence = make_import_evidence(root / "input",
            assets=tuple(AssetMapping(instrument_id=item, local_symbol=item.key, evidence_ref=evidence_ref)
                         for item in instruments),
            benchmark=BenchmarkBinding(instrument_id=benchmark,
                                       return_basis=bundle.manifest.benchmark_type, evidence_ref=evidence_ref),
            source_document="Explicit synthetic benchmark generator identities.",
            table_semantics=standard_table_semantics(bundle, evidence_ref))
        dataset, registration_seconds = timed(lambda: store.register_dataset(root / "input", identity_evidence=evidence))
        members = instruments[:5]
        BindingStore(store).get_asset_binding(dataset.id).resolve(members)
        basket = BasketStore(store).create("storage-benchmark", members, "synthetic ETF selection", "storage-benchmark")
        template = Path(__file__).resolve().parents[1] / "strategies" / "buy_hold"
        declaration = json.loads((template / "strategy.json").read_text(encoding="utf-8"))
        strategy = store.register_strategy({p.name: p.read_bytes() for p in template.iterdir() if p.is_file()}, declaration)
        spec = {"start": str(bundle.calendar.date.iloc[-251].date()),
            "end": str(bundle.manifest.data_end), "initial_cash": 100000, "seed": 7,
            "execution": {"model": "next_open", "commission_rate": 0, "minimum_commission": 0, "slippage_rate": 0, "max_stale_sessions": 0},
            "risk": {"max_total_weight": 1, "max_symbol_weight": 1, "group_caps": {}, "stress_scenarios": [], "pause_action": "block_new", "recovery_mode": "never"},
            "metrics": {"annual_sessions": 252, "risk_free_rate": 0, "cash_rate": 0, "definition_version": "1"}}
        draft = store.save_experiment(None, None, {"framework_contract_version": "multi-asset-v1",
            "basket_revision_ref": basket.ref.model_dump(mode="json"),
            "strategy_revision_id": strategy.id, "dataset_version_id": dataset.id,
            "trading_definition_ref": ETF_TRADING_DEFINITION.ref.model_dump(mode="json"),
            "strategy_params": {}, "backtest_spec": spec, "sample_purpose": "storage benchmark"})
        resolved = FrameworkResolver(store).resolve(FixedRequest.model_validate(draft.payload))
        run_spec = BacktestSpec.model_validate(resolved.internal_backtest_spec)
        builder = SnapshotBuilder(store)
        first, first_seconds = timed(lambda: builder.freeze(draft, "agent", None, resolved))
        _, second_seconds = timed(lambda: builder.freeze(draft, "agent", None, resolved))
        manifest, verification_seconds = timed(lambda: builder.verify(FrozenRun(first.run_id, first.manifest_path, first.manifest_hash), exact=False))
        if manifest["schema_version"] == 1:
            data_path = first.manifest_path.parent / "dataset"
            runtime, runtime_seconds = timed(lambda: loaders.load_dataset(data_path))
        else:
            data_path = store.root / "dataset_objects" / manifest["dataset_ref"]["content_hash"]
            runtime, runtime_seconds = timed(lambda: loaders.load_dataset_for_run(data_path, run_spec))
        expected = bundle.prices[bundle.prices.symbol.isin(run_spec.symbols) & (bundle.prices.date <= pd.Timestamp(run_spec.end))].reset_index(drop=True)
        actual = runtime.prices[runtime.prices.symbol.isin(run_spec.symbols) & (runtime.prices.date <= pd.Timestamp(run_spec.end))].reset_index(drop=True)
        pd.testing.assert_frame_equal(actual, expected)
        print(json.dumps({"platform": platform.platform(), "python": platform.python_version(),
            "rows": args.symbols * args.days, "schema": manifest["schema_version"],
            "method": "synthetic prepared market snapshot; basket, binding and framework resolution outside timed phases; one observation per pipeline phase; OS cache not cleared; not comparable to prior CSV input benchmark; not end-to-end",
            "seconds": {"assembled_load_validate": load_seconds, "register": registration_seconds,
                "freeze_first": first_seconds, "freeze_second": second_seconds,
                "snapshot_verify": verification_seconds, "runtime_load": runtime_seconds},
            "bytes": {"input": size(root / "input"), "registered": size(dataset.path),
                "two_snapshots": size(store.root / "snapshots"), "shared_objects": size(store.root / "dataset_objects")},
            "loaded_price_rows": len(runtime.prices)}, indent=2))


if __name__ == "__main__":
    main()
