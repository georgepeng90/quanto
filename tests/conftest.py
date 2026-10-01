import json

import pytest


@pytest.fixture(autouse=True)
def isolated_market_config(tmp_path, monkeypatch):
    config = tmp_path / "market-config.json"
    config.write_text(json.dumps({"root": str(tmp_path / "shared-market")}), encoding="utf-8")
    monkeypatch.setenv("QUANTO_MARKET_CONFIG", str(config))
    return config
