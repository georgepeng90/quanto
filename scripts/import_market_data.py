"""Administrative import of explicit, evidenced CSV/Parquet market tables."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from quanto.data.catalog import MarketCatalog
from quanto.data.contracts import DataRequest, SourceRecord
from quanto.data.imports import import_table
from quanto.data.providers.base import daily_bars
from quanto.data.storage import MarketStore


def import_file(root: Path, path: Path, metadata: dict):
    original = path.read_bytes()
    mapping = metadata.get("column_map", {})
    if path.suffix.lower() == ".csv":
        frame = pd.read_csv(path, dtype={"symbol": str, "instrument_id": str,
            **{name: str for name, target in mapping.items() if target in {"symbol", "instrument_id"}}})
    elif path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    else:
        raise ValueError("import requires CSV or Parquet")
    request = DataRequest.model_validate(metadata["request"])
    source = SourceRecord.model_validate(metadata["source"])
    frame = frame.rename(columns=mapping)
    date_column = metadata.get("daily_date_column")
    if date_column:
        if request.kind != "bars" or request.frequency != "1d" or len(request.instruments) != 1:
            raise ValueError("daily CSV conversion requires one daily instrument")
        if "symbol" in frame and not frame.symbol.eq(request.instruments[0].code).all():
            raise ValueError("legacy symbol differs from explicit instrument")
        frame = daily_bars(frame.rename(columns={date_column: "date"}), request.instruments[0], request)
    else:
        for column in ("bar_start", "bar_end", "session_start", "session_end", "available_at"):
            if column in frame:
                values = pd.to_datetime(frame[column], format="mixed")
                if values.dt.tz is None and values.notna().any():
                    if not metadata.get("timezone"):
                        raise ValueError("naive input timestamps require explicit timezone")
                    values = values.dt.tz_localize(metadata["timezone"], ambiguous="raise", nonexistent="raise")
                frame[column] = pd.to_datetime(values, utc=True)
        if "instrument_id" in frame and metadata.get("instrument_map"):
            frame["instrument_id"] = frame.instrument_id.replace(metadata["instrument_map"])
    source = source.model_copy(update={"parameters": {**source.parameters, "input_sha256": hashlib.sha256(original).hexdigest(),
        "column_map": mapping, "daily_date_column": date_column, "timezone": metadata.get("timezone")}})
    if path.read_bytes() != original:
        raise ValueError("input changed during import")
    return import_table(MarketStore(root), MarketCatalog(root), request, frame, source, original)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    refs = import_file(args.root, args.input, json.loads(args.metadata.read_text(encoding="utf-8")))
    print(json.dumps([ref.model_dump(mode="json") for ref in refs], ensure_ascii=False))


if __name__ == "__main__":
    main()
