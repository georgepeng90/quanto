from __future__ import annotations

import math


FEATURE_VERSION = "minute-price-volume-v1"
STEP_POLICY = "observed_bar_steps"
FEATURE_NAMES = ("log_return", "log_body", "range_ratio", "log_volume_change", "log_amount_change")


def causal_features(row, previous=None):
    return dict(zip(FEATURE_NAMES, (
        math.log(row["close"]) - math.log(previous["close"]) if previous else 0.,
        math.log(row["close"]) - math.log(row["open"]),
        (row["high"] - row["low"]) / row["open"],
        math.log1p(row["volume"]) - math.log1p(previous["volume"]) if previous else 0.,
        math.log1p(row["amount"]) - math.log1p(previous["amount"]) if previous else 0.,
    )))
