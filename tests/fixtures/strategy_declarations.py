"""Explicit ETF strategy declaration used by registration fixtures."""
import json
from pathlib import Path


def etf_declaration(**changes):
    path = Path(__file__).resolve().parents[2] / "strategies" / "buy_hold" / "strategy.json"
    return {**json.loads(path.read_text(encoding="utf-8")), **changes}
