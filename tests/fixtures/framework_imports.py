"""Declared identities for synthetic dataset imports used by integration tests."""

from pathlib import Path

from quanto.backtest.dataset_loaders import load_dataset
from quanto.data.contracts import InstrumentId
from quanto.backtest.dataset_identity import make_import_evidence, standard_table_semantics
from quanto.framework import AssetMapping, BenchmarkBinding


_DECLARED_ASSETS = {
    "000001": InstrumentId(exchange="SZSE", instrument_type="etf", code="000001"),
    "000002": InstrumentId(exchange="SZSE", instrument_type="etf", code="000002"),
    "512480": InstrumentId(exchange="SSE", instrument_type="etf", code="512480"),
    "159995": InstrumentId(exchange="SZSE", instrument_type="etf", code="159995"),
    "512760": InstrumentId(exchange="SSE", instrument_type="etf", code="512760"),
    "515790": InstrumentId(exchange="SSE", instrument_type="etf", code="515790"),
    "512690": InstrumentId(exchange="SSE", instrument_type="etf", code="512690"),
    "512170": InstrumentId(exchange="SSE", instrument_type="etf", code="512170"),
}


def fixture_identity_evidence(root: Path) -> dict:
    bundle = load_dataset(root)
    symbols = set(bundle.prices.symbol) | set(bundle.instruments.symbol) | set(bundle.status.symbol) | set(bundle.events.symbol)
    for table in bundle.auxiliary.values():
        if "symbol" in table.columns:
            symbols.update(table.symbol)
    unknown = symbols - _DECLARED_ASSETS.keys()
    if unknown:
        raise ValueError(f"Undeclared fixture identities: {sorted(unknown)}")
    evidence_ref = "fixture:declared-identities"
    assets = tuple(AssetMapping(instrument_id=_DECLARED_ASSETS[symbol], local_symbol=symbol,
                                evidence_ref=evidence_ref) for symbol in sorted(symbols))
    return make_import_evidence(root, assets=assets,
        benchmark=BenchmarkBinding(custom_series_id="synthetic-benchmark",
                                   return_basis=bundle.manifest.benchmark_type, evidence_ref=evidence_ref),
        source_document="Explicit synthetic fixture identities and benchmark declaration.",
        table_semantics=standard_table_semantics(bundle, evidence_ref))
