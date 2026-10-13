from dataclasses import replace

import pytest

from quanto.framework import DefinitionRef
from quanto.investing.definitions import ETF_MARKET_FEATURES


def test_registry_exposes_only_implemented_versioned_definitions():
    from quanto.workbench.definitions import DefinitionRegistry

    registry = DefinitionRegistry()
    items = registry.list()
    assert len(items) == 11
    assert sorted(item["ref"]["kind"] for item in items) == ["feature", "feature", *(["learning"] * 7), "trading", "trading"]
    assert sum(item["supported_instrument_types"] == ("stock",) for item in items) == 3
    assert registry.get(ETF_MARKET_FEATURES.ref) is ETF_MARKET_FEATURES
    with pytest.raises(KeyError):
        registry.get(DefinitionRef(kind="feature", id=ETF_MARKET_FEATURES.ref.id, version="missing"))
    with pytest.raises(TypeError):
        items[0]["parameter_schema"]["additionalProperties"] = True


def test_test_injection_does_not_register_placeholder_globally():
    from quanto.workbench.definitions import DefinitionRegistry

    synthetic = replace(ETF_MARKET_FEATURES,
        ref=DefinitionRef(kind="feature", id="fixture", version="1"), supported_instrument_types=("stock",))
    isolated = DefinitionRegistry((synthetic,))
    assert isolated.get(synthetic.ref) is synthetic
    assert len(isolated.list()) == 1
    with pytest.raises(KeyError):
        DefinitionRegistry().get(synthetic.ref)
    with pytest.raises(ValueError, match="duplicate"):
        DefinitionRegistry((synthetic, synthetic))


def test_snapshot_freezes_definition_and_validates_one_parameter_source():
    from quanto.workbench.definitions import DefinitionRegistry

    registry = DefinitionRegistry()
    params = {"lookback": 61}
    snapshot = registry.snapshot(ETF_MARKET_FEATURES, params)
    params["lookback"] = 3
    assert snapshot.effective_params["lookback"] == 61
    assert snapshot.implementation_hash == ETF_MARKET_FEATURES.implementation_hash
    assert snapshot.declaration["definition_hash"] == ETF_MARKET_FEATURES.definition_hash
    assert len(snapshot.declaration["fields"]) == 9
    with pytest.raises(ValueError):
        registry.snapshot(ETF_MARKET_FEATURES, {"lookback": 61, "other": 1})
    with pytest.raises(ValueError, match="registered"):
        registry.snapshot(replace(ETF_MARKET_FEATURES, implementation_hash="a" * 64), {"lookback": 61})
