"""Explicitly synthetic research for screening contract tests."""
from copy import deepcopy


def screening_request():
    identity = {"exchange": "SZSE", "instrument_type": "stock", "code": "000001"}
    citation = {"evidence_id": "source", "quote": "synthetic company facts"}
    return {
        "name": "Synthetic screening", "purpose": "Software verification",
        "as_of": "2026-10-13T16:00:00+08:00", "universe_description": "Explicit synthetic list",
        "universe_kind": "provided_list", "trading_days": ["2026-10-12", "2026-10-13", "2026-10-14"],
        "rules": {"allowed_boards": ["main_board"], "min_listing_days": 365,
                  "liquidity_window": 2, "min_average_amount": 1000, "min_history_days": 2,
                  "excluded_statuses": ["st", "suspended", "delisted"], "industry_max": 1, "target_count": 2},
        "evidence": [{"id": "source", "source_name": "Synthetic fixture", "source_kind": "filing",
                      "source_locator": "https://example.org/synthetic", "text": "synthetic company facts",
                      "published_at": "2026-10-12T08:00:00+08:00", "retrieved_at": "2026-10-13T15:30:00+08:00",
                      "instruments": [identity], "archive_level": "excerpt"}],
        "candidates": [{"instrument_id": identity, "name": "Synthetic bank", "board": "main_board",
                        "industry": "bank", "listing_date": "1991-04-03", "status": "trading",
                        "status_date": "2026-10-13", "citations": {k: [deepcopy(citation)] for k in
                            ("name", "board", "industry", "listing_date", "status", "business_summary")},
                        "daily": [{"date": day, "close": 10, "volume": 1000, "amount": 10000,
                                   "amount_unit": "CNY", "citations": [deepcopy(citation)]}
                                  for day in ("2026-10-12", "2026-10-13")],
                        "recommendation": "include", "rank": 1, "business_summary": "Synthetic banking",
                        "reasons": ["Synthetic research reason"], "risks": ["Synthetic risk"],
                        "counter_evidence": [], "unknowns": []}],
        "agent_model": "test-fixture", "prompt_version": "synthetic-v1", "idempotency_key": "screening-test",
    }
