"""Prepare a declared market requirement before any consumer runs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from quanto.data import MarketData
from quanto.data.contracts import DataRequirement, MarketDataError


def prepare(root, requirement, refresh=False, providers=None):
    requirement = DataRequirement.model_validate(requirement)
    with MarketData(root, providers=providers) as market:
        task = market.prefetch(requirement, refresh=refresh)
        try:
            market.wait_ready(task.id)
        except MarketDataError:
            pass
        return market.get_prefetch(task.id).model_dump(mode="json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--requirement", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    result = prepare(args.root, json.loads(args.requirement.read_text(encoding="utf-8")), args.refresh)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["state"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
