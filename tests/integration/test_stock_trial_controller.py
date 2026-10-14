"""Exercise the durable stock controller against the real API with tiny synthetic jobs."""
import importlib.util
import json
from pathlib import Path

from tests.integration.test_stock_training import stock_workspace


def test_stock_controller_uses_real_framework_model_and_history_contracts(tmp_path, monkeypatch):
    path = Path(__file__).parents[2] / "scripts/run_stock_reward_trial.py"
    spec = importlib.util.spec_from_file_location("stock_trial_controller_integration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROUNDS", 1)
    monkeypatch.setattr(mod, "BUDGET", 4)
    _, client, headers, request = stock_workspace(tmp_path, monkeypatch)
    source = request["spec"]
    source.update(gamma=1, device="cpu")
    models = {}
    for role, (definition, reward) in mod.REWARDS.items():
        models[role] = {"learning_definition_ref": {"kind": "learning", "id": definition, "version": "1"},
                        "spec": {**source, "name": role, "reward_version": reward}}
    dataset = client.get("/api/datasets/" + request["dataset_id"], headers=headers).json()
    protocol = {"dataset_id": request["dataset_id"], "dataset_content_hash": dataset["content_hash"],
        "workspace_path": str(tmp_path / "workspace"), "basket_revision_ref": request["basket_revision_ref"],
        "framework_contract_version": "multi-asset-v1", "trading_definition_ref": request["trading_definition_ref"],
        "models": models, "rounds": 1, "timesteps_per_round": 4, "device": "cpu"}

    class API:
        def get(self, route):
            response = client.get("/api" + route, headers=headers)
            response.raise_for_status()
            return response.json()

        def post(self, route, body):
            response = client.post("/api" + route, headers=headers, json=body)
            response.raise_for_status()
            return response.json()

    with client:
        controller = mod.StockRewardTrial(protocol, tmp_path / "trial", "http://testserver", api=API())
        controller.run()
        assert controller.state["status"] == "complete"
        assert {role: controller.state["roles"][role]["successful_sessions"] for role in mod.ROLES} == {
            "control": 1, "dsr": 1}
        for role in mod.ROLES:
            manifest = (tmp_path / "trial" / role / "round_001.json").read_text(encoding="utf-8")
            assert json.loads(manifest)["metrics"]["test"] is None
        mod.StockRewardTrial(protocol, tmp_path / "trial", "http://testserver", api=API()).run()
