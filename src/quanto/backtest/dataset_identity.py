"""Auditable import identities for daily datasets consumed by the account engine."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from quanto.backtest.dataset_loaders import validate_dataset
from quanto.backtest.dataset_schemas import DatasetBundle
from quanto.framework import AssetBinding, AssetMapping, BenchmarkBinding, FrozenContract, JsonMap, Name, TableSemanticBinding, canonical_hash
from quanto.objects import digest, json_bytes, safe_name


CORE_TABLES = ("prices", "benchmark", "calendar", "instruments", "status", "events")
EVIDENCE_FILE = "identity_evidence.json"


def declared_dataset_files(root: Path) -> dict[str, bytes]:
    root = Path(root)
    if root.is_symlink() or (root / "manifest.json").is_symlink():
        raise ValueError("symbolic links are not snapshot inputs")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    names = {"manifest.json", *manifest.get("auxiliary_tables", {}).values()}
    for table in CORE_TABLES:
        name = next((f"{table}.{suffix}" for suffix in ("parquet", "csv") if (root / f"{table}.{suffix}").exists()), None)
        if name is None:
            raise FileNotFoundError(root / f"{table}.csv")
        names.add(name)
    for name in names:
        safe_name(name)
        path = root / name
        if not path.is_file() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents if root in parent.parents):
            raise ValueError("symbolic links are not snapshot inputs")
    return {name: (root / name).read_bytes() for name in sorted(names)}


def file_digests(files: Mapping[str, bytes]) -> dict[str, str]:
    return {name: digest(value) for name, value in files.items()}


class ImportIdentityEvidence(FrozenContract):
    source_document: Name
    input_files: JsonMap
    assets: Annotated[tuple[AssetMapping, ...], Field(min_length=1)]
    benchmark: BenchmarkBinding
    table_semantics: Annotated[tuple[TableSemanticBinding, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def complete(self):
        # Reuse the exact binding uniqueness and hash validation rules.
        AssetBinding(dataset_version_id="evidence", dataset_content_hash="0" * 64, assets=self.assets,
                     benchmark=self.benchmark, table_semantics=self.table_semantics)
        if not self.input_files:
            raise ValueError("IDENTITY_UNRESOLVED: import evidence needs input file digests")
        for name, value in self.input_files.items():
            safe_name(name)
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("IDENTITY_UNRESOLVED: invalid input file digest")
        return self


class PublishedIdentityEvidence(FrozenContract):
    format: Literal["canonical-import-v1"] = "canonical-import-v1"
    evidence: ImportIdentityEvidence
    output_files: JsonMap

    @property
    def ref(self) -> str:
        return "import:" + canonical_hash(self.model_dump(mode="json"))


def make_import_evidence(root: Path, *, assets: tuple[AssetMapping, ...], benchmark: BenchmarkBinding,
                         source_document: str, table_semantics: tuple[TableSemanticBinding, ...]) -> dict:
    """Freeze an explicit declaration against these exact input bytes; infer no identity."""
    return ImportIdentityEvidence(source_document=source_document, input_files=file_digests(declared_dataset_files(root)),
                                  assets=assets, benchmark=benchmark, table_semantics=table_semantics).model_dump(mode="json")


def standard_table_semantics(bundle: DatasetBundle, evidence_ref: str) -> tuple[TableSemanticBinding, ...]:
    """Declare the existing daily dataset format, including its research assumptions."""
    currency = bundle.manifest.currency
    def price(unit="price"):
        return {"dtype": "float", "unit": unit, "currency": currency}
    symbol = {"dtype": "string", "unit": "instrument_id"}
    day = {"dtype": "date", "unit": "trading_day"}
    schemas = {
        "prices": {"date": day, "symbol": symbol, **{name: price() for name in ("open", "high", "low", "close")},
                   "volume": {"dtype": "float", "unit": bundle.manifest.volume_unit}, "amount": price(bundle.manifest.amount_unit)},
        "benchmark": {"date": day, "close": price("index_level"), "turnover": price(bundle.manifest.amount_unit)},
        "calendar": {"date": day},
        "instruments": {"symbol": symbol, "lot_size": {"dtype": "int", "unit": "share"}, "settlement_sessions": {"dtype": "int", "unit": "session"}, "listing_date": day, "group": {"dtype": "string", "unit": "category"}},
        "status": {"date": day, "symbol": symbol, "status": {"dtype": "string", "unit": "category"}, **{name: {"dtype": "bool", "unit": "flag"} for name in ("can_buy", "can_sell")}},
        "events": {"symbol": symbol, "event_type": {"dtype": "string", "unit": "category"}, **{name: day for name in ("record_date", "ex_date", "pay_date")}, "cash_per_share": price("cash_per_share"), "split_ratio": {"dtype": "float", "unit": "ratio"}},
    }
    if "board" in bundle.instruments:
        schemas["instruments"]["board"] = {"dtype": "string", "unit": "category"}
    rules = {
        "prices": {"availability_rule": "session_close", "date_column": "date", "assumption": "daily OHLCV complete at close; next open execution only"},
        "benchmark": {"availability_rule": "session_close", "date_column": "date"},
        "calendar": {"availability_rule": "declared_calendar"},
        "instruments": {"availability_rule": "declared_static_reference", "assumption": "dataset reference facts; not historical disclosure evidence"},
        "status": {"availability_rule": "session_close", "date_column": "date", "assumption": "research trading flags" if bundle.price_mode == "adjusted_research" else "source trading flags"},
        "events": {"availability_rule": "execution_event", "date_column": "ex_date", "assumption": "execution ledger only; no disclosure timestamp evidence"},
    }
    return tuple(TableSemanticBinding(table=name, semantic_id=f"daily-{name}", version="1", schema_definition=schemas[name],
                                      temporal_rules={**rules[name], "timezone": "Asia/Shanghai", "price_mode": bundle.price_mode}, evidence_ref=evidence_ref)
                 for name in CORE_TABLES)


def validate_binding_tables(bundle: DatasetBundle, assets: tuple[AssetMapping, ...], semantics: tuple[TableSemanticBinding, ...]) -> None:
    if set(bundle.auxiliary) & set(CORE_TABLES):
        raise ValueError("BINDING_MISMATCH: auxiliary names cannot shadow core tables")
    symbols = {asset.local_symbol for asset in assets}
    if symbols != set(bundle.instruments.symbol):
        raise ValueError("BINDING_MISMATCH: identity mapping must cover all dataset assets")
    for name, frame in {"prices": bundle.prices, "instruments": bundle.instruments, "status": bundle.status, "events": bundle.events, **bundle.auxiliary}.items():
        for column in ("symbol", "instrument_id"):
            if column in frame and (frame[column].isna().any() or not set(frame[column]).issubset(symbols)):
                raise ValueError(f"BINDING_MISMATCH: unknown asset in {name}.{column}")
        if {"symbol", "instrument_id"}.issubset(frame.columns) and not frame.symbol.equals(frame.instrument_id):
            raise ValueError(f"BINDING_MISMATCH: conflicting identity columns in {name}")
    if {item.table for item in semantics} != {*CORE_TABLES, *bundle.auxiliary}:
        raise ValueError("IDENTITY_UNRESOLVED: every table needs a frozen semantic declaration")
    for item in semantics:
        frame = getattr(bundle, item.table) if item.table in CORE_TABLES else bundle.auxiliary[item.table]
        if not set(item.schema_definition).issubset(frame.columns):
            raise ValueError(f"BINDING_MISMATCH: declared columns absent in {item.table}")


def canonicalize_import(bundle: DatasetBundle, evidence: ImportIdentityEvidence) -> DatasetBundle:
    validate_binding_tables(bundle, evidence.assets, evidence.table_semantics)
    mapping = {item.local_symbol: item.instrument_id.key for item in evidence.assets}
    def convert(frame):
        result = frame.copy(deep=True)
        for column in ("symbol", "instrument_id"):
            if column in result:
                result[column] = result[column].map(mapping)
        return result
    return validate_dataset(replace(bundle, prices=convert(bundle.prices), instruments=convert(bundle.instruments),
                                    status=convert(bundle.status), events=convert(bundle.events),
                                    auxiliary={name: convert(frame) for name, frame in bundle.auxiliary.items()}))


def publish_import_evidence(destination: Path, evidence: ImportIdentityEvidence) -> PublishedIdentityEvidence:
    published = PublishedIdentityEvidence(evidence=evidence, output_files=file_digests(declared_dataset_files(destination)))
    (destination / EVIDENCE_FILE).write_bytes(json_bytes(published.model_dump(mode="json")))
    return published


def import_binding(dataset_id: str, content_hash: str, root: Path, bundle: DatasetBundle) -> AssetBinding:
    path = root / EVIDENCE_FILE
    if not path.is_file() or path.is_symlink():
        raise ValueError("IDENTITY_UNRESOLVED: dataset has no registered import evidence")
    published = PublishedIdentityEvidence.model_validate_json(path.read_bytes())
    if dict(published.output_files) != file_digests(declared_dataset_files(root)):
        raise ValueError("BINDING_MISMATCH: import output file hash mismatch")
    evidence, ref = published.evidence, published.ref
    assets = tuple(AssetMapping(instrument_id=item.instrument_id, local_symbol=item.instrument_id.key, evidence_ref=ref) for item in evidence.assets)
    semantics = tuple(TableSemanticBinding(**{**item.model_dump(mode="json", exclude={"declaration_hash"}), "evidence_ref": ref}) for item in evidence.table_semantics)
    validate_binding_tables(bundle, assets, semantics)
    benchmark = BenchmarkBinding(**{**evidence.benchmark.model_dump(mode="json"), "evidence_ref": ref})
    if benchmark.return_basis != bundle.manifest.benchmark_type:
        raise ValueError("BINDING_MISMATCH: benchmark return basis differs")
    return AssetBinding(dataset_version_id=dataset_id, dataset_content_hash=content_hash, assets=assets,
                        benchmark=benchmark, table_semantics=semantics)
