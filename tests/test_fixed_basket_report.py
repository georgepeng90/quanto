"""Report budget, inherited final, and CUDA boundary contracts; no live training."""
import importlib.util
from pathlib import Path

import pytest


def module():
    path = Path(__file__).parents[1] / "scripts/report_fixed_basket_training.py"
    spec = importlib.util.spec_from_file_location("fixed_basket_report", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_original_fifty_round_evaluation_contract_is_preserved():
    report = module()
    protocol = {"rounds": 50}
    keys = report.evaluation_keys(protocol)
    assert len(keys) == 16
    assert report.monitoring_checkpoints(protocol) == (10, 20, 30, 40, 50)
    assert report.evaluation_role("final_buy_hold", 50) == ("final", 50, "buy_hold", False)


def test_extension_final_keys_cannot_be_mistaken_for_original_final():
    report = module()
    keys = report.evaluation_keys({"rounds": 250}, True)
    assert len(keys) == 60
    assert "final_250_last" in keys and "historical_final_050_last" in keys
    assert "final_last" not in keys
    assert report.evaluation_role("final_250_equal_weight", 250) == ("final", 250, "equal_weight", False)
    assert report.evaluation_role("historical_final_050_best", 250) == ("final", 50, "best", True)
    assert report.evaluation_role("monitor_060_last", 250) == ("monitor", 60, "last", False)


@pytest.mark.parametrize("number,device", [(1, "cpu"), (50, "cpu"), (51, "cuda"), (250, "cuda")])
def test_manifest_and_runtime_must_match_cpu_cuda_boundary(number, device):
    report = module()
    protocol = {"spec": {"device": "cpu", "seed": 42}, "device_override": "cuda"}
    manifest = {"config": {"device": device, "seed": 42},
                "runtime": {"device": device, "cuda_version": "12.8" if device == "cuda" else None}}
    report.verify_session_device(protocol, True, number, manifest)
    manifest["runtime"]["device"] = "cpu" if device == "cuda" else "cuda"
    with pytest.raises(ValueError, match="runtime device"):
        report.verify_session_device(protocol, True, number, manifest)


def test_cuda_config_without_cuda_runtime_evidence_is_rejected():
    protocol = {"spec": {"device": "cpu"}, "device_override": "cuda"}
    with pytest.raises(ValueError, match="CUDA runtime evidence"):
        module().verify_session_device(protocol, True, 51,
            {"config": {"device": "cuda"}, "runtime": {"device": "cuda", "cuda_version": None}})


def test_cuda_extension_rejects_cpu_config_even_with_cuda_runtime_claim():
    protocol = {"spec": {"device": "cpu"}, "device_override": "cuda"}
    with pytest.raises(ValueError, match="spec.device: changed"):
        module().verify_session_device(protocol, True, 51,
            {"config": {"device": "cpu"}, "runtime": {"device": "cuda", "cuda_version": "12.8"}})
