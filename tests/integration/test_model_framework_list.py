from quanto.investing.models import InvestingModels
from quanto.workbench.bindings import BindingStore
from quanto.workbench.service import WorkbenchService
from tests.integration.test_framework_capabilities import _training_case
from tests.integration.test_model_framework_bindings import _old_model


def test_model_list_exposes_registered_identity_without_auditing_legacy_files(tmp_path, monkeypatch):
    store, request, _ = _training_case(tmp_path)
    _old_model(store, request, model_id="unbound")
    _old_model(store, request, model_id="bound")
    binding = BindingStore(store).bind_model("bound", "bind")

    def no_audit(*args, **kwargs):
        raise AssertionError("list must not audit checkpoint files")

    monkeypatch.setattr(BindingStore, "inspect_model_binding", no_audit)
    items = {item["id"]: item for item in InvestingModels(WorkbenchService(store.root)).list_models()["items"]}
    assert items["unbound"]["framework_binding"] is None
    assert items["unbound"]["framework_binding_check"] is None
    assert items["bound"]["framework_binding"]["basket_revision_ref"] == binding.basket.ref.model_dump(mode="json")
    assert items["bound"]["framework_binding"]["ordered_pairs"] == [pair.model_dump(mode="json") for pair in binding.ordered_pairs]
