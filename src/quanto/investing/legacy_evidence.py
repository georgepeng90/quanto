"""Packaged, fixed-input ETF equivalence record for audited historical models.

These hashes identify the definitions validated against the frozen T00 traces by
the T06 bound-environment tests. They are a release validation record, not a
historical source hash for any individual training job or a per-bind replay.
"""
from __future__ import annotations


FROZEN_ETF_BASELINE_SHA256 = "753d687e104686fc4948de72475489a1c88379ace1d78cfe18689b08e1d462ca"
FROZEN_ETF_SOURCE_COMMIT = "2985bd099ecfe9dd53abc0dcbe727b7cff8c1d03"
_VALIDATED_FEATURE_HASH = "0654b64f097672c2611aa146159a9ac626e8b20a5935633ba6e6ac667b23a969"
_VALIDATED_TRADING_HASH = "450cadecaf15b97f1dfa9dfbe0c914aecc6b22229d8ddfaa42ae8c81da094f47"
_VALIDATED_LEARNING_HASHES = {
    ("hold-target-quartiles-v1", "net-log-return-v1"):
        "174e5266a4e5bf7370ceb988bfd3e8cefcafb5bbc544367c28b7a012c1ec07f6",
    ("hold-target-quartiles-v1", "net-log-return-with-state-v1"):
        "efada7cb4dabe5699b36722c6bf528f2f880c9f465b793bfec2b6758e8065302",
    ("hold-target-quartiles-v1", "differential-sharpe-v1"):
        "a1be1066666e89fce4968842307046d825dd975861eae45ee03310001376fcd5",
    ("hold-target-cash-quartiles-v2", "net-log-return-v1"):
        "21c75cf46c91642fd8a028ededdf03c0c2c4fabd77fc6f4be6e7a6f06a4ed473",
    ("hold-target-cash-quartiles-v2", "net-log-return-with-state-v1"):
        "42c99a77e74c2bd88d7a4f55af405200071b680e85621aeb211c69362c979f3d",
    ("hold-target-cash-quartiles-v2", "differential-sharpe-v1"):
        "c48cbeae4865525c739b880596a0311312e334eac6fb023ddc17eefc677bab7f",
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
