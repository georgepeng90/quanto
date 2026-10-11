from quanto.workbench.results import compare_runs


def test_comparison_reports_all_material_differences():
    runs = [
        {"run_id": "a", "state": "succeeded", "dataset_version_id": "d1", "benchmark": "b1", "start": "2024-01-01", "end": "2024-01-31", "costs": {"commission": 0}, "price_mode": "raw_cash", "metric_definitions": {"return": "1"}, "strategy_source_hashes": {"a.py": "1"}, "engine_source_hashes": {"engine.py": "1"}, "environment_fingerprint": "e1", "seed": 1, "risk": {"cap": 1}, "initial_cash": 1000},
        {"run_id": "b", "state": "succeeded", "dataset_version_id": "d2", "benchmark": "b2", "start": "2024-02-01", "end": "2024-02-28", "costs": {"commission": 1}, "price_mode": "adjusted_research", "metric_definitions": {"return": "2"}, "strategy_source_hashes": {"a.py": "2"}, "engine_source_hashes": {"engine.py": "2"}, "environment_fingerprint": "e2", "seed": 2, "risk": {"cap": 2}, "initial_cash": 2000},
    ]
    result = compare_runs(runs)
    assert {"dataset_version_id", "benchmark", "start", "end", "costs", "price_mode", "metric_definitions"} <= set(result.differences)
    assert {"strategy_source_hashes", "engine_source_hashes", "environment_fingerprint", "seed", "risk", "initial_cash"} <= set(result.differences)
    assert not result.directly_comparable


def test_method_specific_metric_does_not_erase_shared_economic_comparison():
    shared = {"state": "succeeded", "comparison_key": "same-conditions"}
    fixed = {**shared, "method": "fixed", "metric_definitions": {"total_return": {"formula": "equity/end"}}}
    policy = {**shared, "method": "PPO", "metric_definitions": {
        "total_return": {"formula": "equity/end"}, "average_cash_ratio": {"formula": "mean(cash/equity)"}}}
    assert compare_runs([fixed, policy]).directly_comparable is True
    changed = {**policy, "metric_definitions": {"total_return": {"formula": "gross/equity"}}}
    assert compare_runs([fixed, changed]).directly_comparable is False
