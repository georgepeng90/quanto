import pytest

from quanto.investing.models import InvestingModels
from quanto.workbench.etf_model_migration import ModelMigrator
from quanto.workbench.service import WorkbenchService
from tests.integration.test_framework_capabilities import _training_case
from tests.integration.test_model_framework_bindings import _old_model


def test_model_list_requires_migrated_binding_and_reads_registered_identity(tmp_path):
    store, request, _ = _training_case(tmp_path)
    _old_model(store, request, model_id="unbound")
    _old_model(store, request, model_id="bound")
    binding = ModelMigrator(store).migrate_model("bound")
    service = InvestingModels(WorkbenchService(store.root))
    with pytest.raises(ValueError, match="framework binding missing"):
        service.list_models()
    ModelMigrator(store).migrate_model("unbound")
    items = {item["id"]: item for item in service.list_models()["items"]}
    assert items["bound"]["framework_binding"]["basket_revision_ref"] == binding.basket.ref.model_dump(mode="json")
    assert items["bound"]["framework_binding"]["ordered_pairs"] == [pair.model_dump(mode="json") for pair in binding.ordered_pairs]
