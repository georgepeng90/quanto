"""Packaged, fixed-input ETF equivalence record for audited historical models.

These hashes identify the definitions validated against the frozen T00 traces by
the T06 bound-environment tests. They are a release validation record, not a
historical source hash for any individual training job or a per-bind replay.
"""
from __future__ import annotations


FROZEN_ETF_BASELINE_SHA256 = "753d687e104686fc4948de72475489a1c88379ace1d78cfe18689b08e1d462ca"
FROZEN_ETF_SOURCE_COMMIT = "2985bd099ecfe9dd53abc0dcbe727b7cff8c1d03"
_VALIDATED_FEATURE_HASH = "f2158b7514536b857a73eb34e86cbf7a6c61e770844bea36aff9e3bd638f893d"
_VALIDATED_TRADING_HASH = "450cadecaf15b97f1dfa9dfbe0c914aecc6b22229d8ddfaa42ae8c81da094f47"
_VALIDATED_LEARNING_HASHES = {
    ("hold-target-quartiles-v1", "net-log-return-v1"):
        "dcc1b45e3d0d5495a58638fafac7ea5092b5ef9a108546db30086eff3475dc4e",
    ("hold-target-quartiles-v1", "net-log-return-with-state-v1"):
        "31620893f19c411f6f729c3931dc5bdc0a6b963c85e8ce101c4bdf64e41cd634",
    ("hold-target-quartiles-v1", "differential-sharpe-v1"):
        "1176c533687d803779aba2e1ebe03ec4a1c82ec987b23ef178f778e68171fc7e",
    ("hold-target-cash-quartiles-v2", "net-log-return-v1"):
        "ecabea649be75b950da5558ffc858826d31a6263c64c65805ff609ee6819f3f4",
    ("hold-target-cash-quartiles-v2", "net-log-return-with-state-v1"):
        "628fb18fae9a5275d3e93f0e394b01f98ac9d6c25d2fab69d3c6f93d05a836c8",
    ("hold-target-cash-quartiles-v2", "differential-sharpe-v1"):
        "a5176164daf96ec6c62f037dc4ee362055a5b35c3fc5bdd976b86a82c93b5dbd",
}


def audited_etf_semantics(learning, trading) -> dict[str, object]:
    """Require the exact definition implementations covered by T00/T06 evidence."""
    combination = (learning.action_version, learning.reward_version)
    if (_VALIDATED_LEARNING_HASHES.get(combination) != learning.definition_hash
            or learning.feature_definition.definition_hash != _VALIDATED_FEATURE_HASH
            or trading.definition_hash != _VALIDATED_TRADING_HASH):
        raise ValueError("DEFINITION_UNAVAILABLE: ETF semantic implementation differs from validated evidence")
    return {
        "baseline_sha256": FROZEN_ETF_BASELINE_SHA256,
        "source_commit": FROZEN_ETF_SOURCE_COMMIT,
        "validated_definition_hashes": {
            "learning": learning.definition_hash,
            "feature": learning.feature_definition.definition_hash,
            "trading": trading.definition_hash,
        },
        "validation_scope": "T00 frozen input traces; T06 observation, action, reward and ledger equivalence",
        "limitation": "Release validation record; not original model source or a per-bind replay",
    }
