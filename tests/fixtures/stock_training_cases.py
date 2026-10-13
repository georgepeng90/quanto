"""Explicitly synthetic stock facts; never evidence of real market coverage."""
import base64
import hashlib
import json

import pandas as pd

from quanto.backtest.dataset_identity import make_import_evidence, standard_table_semantics
from quanto.backtest.dataset_loaders import load_dataset
from quanto.data.contracts import InstrumentId
from quanto.framework import AssetBinding, AssetMapping, BenchmarkBinding, TableSemanticBinding
from tests.fixtures.investing_training_cases import investing_case


def stock_case(root):
    spec, dates = investing_case(root)
    instruments = pd.read_csv(root / "instruments.csv", dtype={"symbol": str})
    instruments["board"] = "main_board"
    instruments.to_csv(root / "instruments.csv", index=False)
    payload = b"Synthetic fee, opening status and complete no-event coverage for test only."
    source = dict(source_ref="synthetic", source_url="https://example.org/synthetic",
                  payload_base64=base64.b64encode(payload).decode(), payload_sha256=hashlib.sha256(payload).hexdigest(),
                  published_at="2023-12-31T00:00:00Z", retrieved_at="2024-06-01T00:00:00Z",
                  available_at="2023-12-31T00:00:00Z", coverage_start="2024-01-01", coverage_end="2024-12-31")
    pd.DataFrame([source]).to_csv(root / "stock_sources.csv", index=False)
    pd.DataFrame([dict(exchange="SZSE", instrument_type="stock", board="main_board", side=side,
        effective_from="2024-01-01", effective_to="2025-01-01", stamp_duty_rate=0 if side == "buy" else .0005,
        transfer_fee_rate=.00001, available_at=source["available_at"], source_ref="synthetic")
        for side in ("buy", "sell")]).to_csv(root / "fee_rules.csv", index=False)
    pd.DataFrame(dict(symbol="000001", date=dates, status="trading", can_buy=True, can_sell=True,
                      available_at=[f"{d.date()}T09:00:00+08:00" for d in dates], source_ref="synthetic"
                      )).to_csv(root / "stock_open_status.csv", index=False)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["auxiliary_tables"] = {name: name + ".csv" for name in ("fee_rules", "stock_open_status", "stock_sources")}
    manifest["field_semantics"]["stock_event_coverage_source"] = "synthetic"
    manifest["limitations"] = ["Dividends are recorded before personal dividend tax."]
    (root / "manifest.json").write_text(json.dumps(manifest))
    spec.update(action_version="hold-target-cash-quartiles-v2", reward_version="net-log-return-v1",
                n_steps=2, batch_size=2, n_epochs=1, hidden_size=8, total_timesteps=4, eval_every_timesteps=4)
    return spec, dates


def stock_binding(bundle):
    ref = "fixture:synthetic-stock"
    semantics = list(standard_table_semantics(bundle, ref))
    for name, semantic_id, keys in (("fee_rules", "stock-fee-rules", ("exchange", "board", "side", "effective_from")),
                                    ("stock_open_status", "stock-open-status", ("symbol", "date")),
                                    ("stock_sources", "stock-sources", ("source_ref",))):
        semantics.append(TableSemanticBinding(table=name, semantic_id=semantic_id, version="1",
            schema_definition={column: {"dtype": "string", "unit": "reference"} for column in bundle.auxiliary[name]},
            temporal_rules={"availability_rule": "execution_event", "business_keys": keys,
                            "assumption": "Execution evidence only; not an observation feature."}, evidence_ref=ref))
    return AssetBinding(dataset_version_id="synthetic-stock", dataset_content_hash="0" * 64,
        assets=(AssetMapping(local_symbol="000001", instrument_id=InstrumentId(exchange="SZSE", instrument_type="stock", code="000001"), evidence_ref=ref),),
        benchmark=BenchmarkBinding(custom_series_id="synthetic", return_basis="price_index", evidence_ref=ref),
        table_semantics=tuple(semantics))


def stock_identity_evidence(root):
    binding = stock_binding(load_dataset(root))
    return make_import_evidence(root, assets=binding.assets, benchmark=binding.benchmark,
                                source_document="Synthetic stock fixture", table_semantics=binding.table_semantics)
