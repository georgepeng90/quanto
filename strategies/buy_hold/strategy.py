from __future__ import annotations

import json
from pathlib import Path

from quanto.strategies import validate_parameters
from quanto.strategies.buy_hold import BuyHold


def create_strategy(params: dict) -> BuyHold:
    manifest = json.loads(Path(__file__).with_name("strategy.json").read_text(encoding="utf-8"))
    validate_parameters(manifest["parameter_schema"], params)
    return BuyHold()
