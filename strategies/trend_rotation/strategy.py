from __future__ import annotations

import json
from pathlib import Path

from quanto.strategies import validate_parameters
from quanto.strategies.trend_rotation import TrendRotation


def create_strategy(params: dict) -> TrendRotation:
    manifest = json.loads(Path(__file__).with_name("strategy.json").read_text(encoding="utf-8"))
    effective = validate_parameters(manifest["parameter_schema"], params)
    return TrendRotation(**effective)
