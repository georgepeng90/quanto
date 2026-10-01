"""Export a new audited daily research dataset from a prepared market snapshot.

Historical CSVs require explicit import metadata first. Existing frozen research
outputs are never overwritten by this entry point.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from quanto.backtest.market_dataset import assemble_dataset
from quanto.backtest.dataset_storage import write_parquet_dataset
from quanto.data import MarketData, get_market_root
from quanto.data.contracts import DataRequirement


def prepare_from_snapshot(root, snapshot_id, requirement, output, assumptions):
    requirement = DataRequirement.model_validate(requirement)
    output = Path(output)
    if (output / "dataset").exists() or (output / "source_audit.json").exists():
        raise FileExistsError("choose a new output; frozen research artifacts are immutable")
    with MarketData(root, providers=()) as market:
        ref = market.resolve_snapshot(snapshot_id)
        if ref.quality["requirement_key"] != requirement.key:
            raise ValueError("requirement differs from prepared snapshot")
        bundle = assemble_dataset(market, ref, requirement, assumptions)
    output.mkdir(parents=True, exist_ok=True)
    write_parquet_dataset(bundle, output / "dataset")
    audit = {"market_snapshot_ref": ref.model_dump(mode="json"), "requirement": requirement.model_dump(mode="json"),
        "consumer_assumptions": assumptions, "price_rows": len(bundle.prices), "benchmark_rows": len(bundle.benchmark),
        "limitations": bundle.manifest.limitations, "validation": "passed"}
    (output / "source_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--requirement", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assumptions", type=Path)
    args = parser.parse_args()
    assumptions = json.loads(args.assumptions.read_text(encoding="utf-8")) if args.assumptions else {}
    result = prepare_from_snapshot(get_market_root(), args.snapshot, json.loads(args.requirement.read_text(encoding="utf-8")), args.output, assumptions)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
