from datetime import datetime, timezone

import pytest

from quanto.framework import canonical_hash
from tests.integration.test_framework_capabilities import _training_case


def test_model_binding_freezes_one_training_identity(tmp_path):
    from quanto.framework import ModelFrameworkBinding
    from quanto.workbench.configuration import FrameworkResolver

    store, request, _ = _training_case(tmp_path)
    resolved = FrameworkResolver(store).resolve(request)
    spec = dict(resolved.internal_training_spec)
    binding = ModelFrameworkBinding(model_id="model", original_spec=spec,
        training_configuration=resolved, semantic_identity={"action_version": request.spec.action_version},
        evidence={"method": "resolved-new-model"}, created_at=datetime.now(timezone.utc))
    assert binding.original_spec_hash == canonical_hash(spec)
    assert binding.basket == resolved.basket
    assert binding.asset_binding == resolved.asset_binding
    assert binding.dataset_version_id == resolved.asset_binding.dataset_version_id
    assert binding.ordered_pairs == resolved.ordered_pairs
    assert binding.binding_hash
    with pytest.raises(TypeError):
        binding.original_spec["lookback"] = 3
    with pytest.raises(ValueError, match="spec"):
        ModelFrameworkBinding.model_validate({**binding.model_dump(mode="json"), "original_spec_hash": "a" * 64})
    values = binding.model_dump(mode="json")
    values["evidence"]["method"] = "changed"
    with pytest.raises(ValueError, match="binding_hash"):
        ModelFrameworkBinding.model_validate(values)
