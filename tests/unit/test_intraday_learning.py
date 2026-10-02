import csv
import json
from datetime import datetime, timedelta, timezone

import pytest


def bar(day=5, hour=1, minute=31, symbol="A", price=10, close=None, volume=10000, score=1):
    end = datetime(2024, 1, day, hour, minute, tzinfo=timezone.utc)
    close = price if close is None else close
    return dict(instrument_id=symbol, trading_date=f"2024-01-{day:02}",
                bar_start=(end - timedelta(minutes=1)).isoformat(), bar_end=end.isoformat(),
                open=price, high=max(price, close), low=min(price, close), close=close,
                volume=volume, amount=volume * close, score=score)


def run(tmp_path, rows, metadata=None, **changes):
    from quanto.learning.intraday import run_intraday_backtest

    spec = dict(start="2024-01-05T00:00:00+00:00", end="2024-01-12T00:00:00+00:00",
                initial_cash=1000, top_k=1, rebalance_every=1, max_total_weight=1,
                max_symbol_weight=1, commission_rate=0, minimum_commission=0,
                slippage_rate=0, participation_rate=1, settlement_days=1,
                lot_size=1, drawdown_pause=1, cash_rate=0, baseline="model")
    spec.update(changes)
    return run_intraday_backtest(iter(rows), spec, tmp_path, lambda event: None,
                                lambda: False, metadata or {})


def table(path, name):
    with (path / f"{name}.csv").open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_next_open_has_full_timestamp_and_exact_account(tmp_path):
    result = run(tmp_path, [bar(price=10), bar(minute=32, price=20, close=25, score=-1)])
    fills = table(tmp_path, "fills")
    assert len(fills) == 1
    assert fills[0]["timestamp"] == "2024-01-05T01:31:00+00:00"
    assert fills[0]["signal_time"] == "2024-01-05T01:31:00+00:00"
    assert int(fills[0]["quantity"]) == 50
    assert float(fills[0]["price"]) == 20
    assert result["metrics"]["final_equity"] == 1250
    assert table(tmp_path, "orders")[-1]["reason"] == "end_of_period"
    assert json.loads((tmp_path / "summary.json").read_text())["metrics"] == result["metrics"]


def test_t1_blocks_same_day_sale_and_weekend_settles_next_observed_day(tmp_path):
    rows = [bar(), bar(minute=32, score=-1), bar(minute=33, score=-1),
            bar(day=8, price=11, score=-1)]
    result = run(tmp_path, rows)
    fills = table(tmp_path, "fills")
    assert [(f["side"], int(f["quantity"])) for f in fills] == [("buy", 100), ("sell", 100)]
    assert fills[-1]["trading_date"] == "2024-01-08"
    assert any(o["reason"] == "settlement" for o in table(tmp_path, "orders"))
    assert result["metrics"]["final_equity"] == 1100
    assert result["counts"]["trading_days"] == 2


def test_lunch_and_overnight_use_next_real_bar_without_synthetic_bars(tmp_path):
    rows = [bar(hour=3, minute=30), bar(hour=5, minute=1, score=-1),
            bar(day=8, hour=1, minute=31, price=12, score=-1)]
    result = run(tmp_path, rows)
    assert [f["timestamp"] for f in table(tmp_path, "fills")] == [
        "2024-01-05T05:00:00+00:00", "2024-01-08T01:30:00+00:00"]
    assert result["counts"]["bars"] == 3
    assert result["metrics"]["final_equity"] == 1200


def test_fill_cap_uses_previous_volume_and_remainder_expires(tmp_path):
    result = run(tmp_path, [bar(volume=250), bar(minute=32, volume=999999)],
                 lot_size=10, participation_rate=.1)
    assert int(table(tmp_path, "fills")[0]["quantity"]) == 20
    order = table(tmp_path, "orders")[0]
    assert order["status"] == "partial"
    assert order["reason"] == "participation"
    assert int(order["remaining_quantity"]) == 80
    assert result["metrics"]["final_equity"] == 1000


def test_minimum_fee_slippage_cash_and_lot_are_hand_calculated(tmp_path):
    result = run(tmp_path, [bar(), bar(minute=32)], minimum_commission=5,
                 slippage_rate=.01, lot_size=10)
    fill = table(tmp_path, "fills")[0]
    assert int(fill["quantity"]) == 90
    assert float(fill["price"]) == pytest.approx(10.1)
    assert float(fill["commission"]) == 5
    assert result["metrics"]["final_cash"] == pytest.approx(86)
    assert result["metrics"]["final_equity"] == pytest.approx(986)


def test_total_cap_prevents_locked_t1_holding_plus_new_top_pick(tmp_path):
    rows = []
    for minute, scores in [(31, (2, -1)), (32, (-1, 2)), (33, (-1, 2))]:
        rows += [bar(minute=minute, symbol=symbol, score=score)
                 for symbol, score in zip(("A", "B"), scores)]
    run(tmp_path, rows, top_k=2, max_total_weight=.5, max_symbol_weight=.5)
    assert [(f["instrument_id"], int(f["quantity"])) for f in table(tmp_path, "fills")] == [("A", 50)]
    assert any(o["reason"] == "weight_cap" for o in table(tmp_path, "orders"))


def test_cash_interest_once_per_trading_day_and_metrics_use_daily_clock(tmp_path):
    rows = [bar(), bar(minute=32), bar(day=8), bar(day=8, minute=32)]
    result = run(tmp_path, rows, baseline="cash", cash_rate=.252)
    assert result["metrics"]["final_equity"] == pytest.approx(1002.001)
    daily = table(tmp_path, "daily_equity")
    assert [float(d["equity"]) for d in daily] == pytest.approx([1001, 1002.001])
    assert result["metrics"]["annualized_return"] == pytest.approx(1.001 ** 252 - 1)
    assert len(table(tmp_path, "equity")) == 4


def test_end_boundary_does_not_execute_outside_interval(tmp_path):
    run(tmp_path, [bar(), bar(minute=32)], end="2024-01-05T01:31:00+00:00")
    assert table(tmp_path, "fills") == []
    assert len(table(tmp_path, "equity")) == 1
    assert table(tmp_path, "orders")[0]["reason"] == "end_of_period"


def test_future_bar_changes_do_not_change_prior_signal_or_order(tmp_path):
    paths = (tmp_path / "first", tmp_path / "second")
    for path, future in zip(paths, (bar(minute=33, price=5, volume=1, score=-2),
                                  bar(minute=33, price=100, volume=999999, score=99))):
        run(path, [bar(), bar(minute=32, score=-1), future])
    assert table(paths[0], "signals")[:2] == table(paths[1], "signals")[:2]
    assert table(paths[0], "fills")[0] == table(paths[1], "fills")[0]


@pytest.mark.parametrize("changes", [dict(top_k=True), dict(lot_size=0),
                                     dict(cash_rate=float("nan")), dict(participation_rate=2),
                                     dict(start="2024-01-05"), dict(unknown=1)])
def test_invalid_spec_rejected_before_outputs(tmp_path, changes):
    with pytest.raises(ValueError):
        run(tmp_path, [bar()], **changes)
    assert not (tmp_path / "summary.json").exists()


def test_calendar_settles_t2_when_instrument_is_absent_but_sessions_are_observed(tmp_path):
    run(tmp_path, [bar(), bar(minute=32, score=-1), bar(day=8, symbol="B", score=-1),
                   bar(day=9, symbol="B", score=-1), bar(day=10, score=-1)],
        settlement_days=2, metadata={"trading_days": ["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10"]})
    assert [f["side"] for f in table(tmp_path, "fills")] == ["buy", "sell"]


def test_drawdown_pause_liquidates_available_position_and_does_not_reenter(tmp_path):
    run(tmp_path, [bar(), bar(minute=32, close=8), bar(minute=33, price=8),
                   bar(minute=34, price=10)], settlement_days=0, drawdown_pause=.1)
    assert [f["side"] for f in table(tmp_path, "fills")] == ["buy", "sell"]
    assert table(tmp_path, "signals")[-1]["reason"] == "drawdown_pause"


def test_equal_weight_and_momentum_share_execution_constraints(tmp_path):
    rows = [bar(minute=minute, symbol=symbol, close=close, score=None)
            for minute, close in [(31, 10), (32, 11), (33, 11)] for symbol in ("A", "B")]
    equal = tmp_path / "equal"
    run(equal, rows, baseline="equal_weight", top_k=2, max_symbol_weight=.5)
    assert {f["instrument_id"] for f in table(equal, "fills")} == {"A", "B"}
    momentum = tmp_path / "momentum"
    run(momentum, rows, baseline="momentum", top_k=2)
    assert all(f["signal_time"] == "2024-01-05T01:32:00+00:00"
               for f in table(momentum, "fills"))


def test_unsorted_and_duplicate_rows_are_rejected(tmp_path):
    for index, rows in enumerate(([bar(minute=32), bar()], [bar(), bar()])):
        with pytest.raises(ValueError, match="sorted|duplicate"):
            run(tmp_path / str(index), rows)


def test_locked_positions_count_toward_actual_top_k_limit(tmp_path):
    rows = [bar(minute=minute, symbol=symbol, score=score)
            for minute, scores in [(31, (2, 1)), (32, (1, 2)), (33, (1, 2))]
            for symbol, score in zip(("A", "B"), scores)]
    run(tmp_path, rows, max_symbol_weight=.5)
    assert [(f["instrument_id"], int(f["quantity"])) for f in table(tmp_path, "fills")] == [("A", 50)]
    assert any(o["reason"] == "top_k" for o in table(tmp_path, "orders"))


def test_opening_gap_pauses_pending_buys_before_they_execute(tmp_path):
    rows = [bar(minute=minute, symbol=symbol, price=price, score=score)
            for minute, entries in [(31, (("A", 10, 1), ("B", 10, None))),
                                    (32, (("A", 10, 1), ("B", 10, 1))),
                                    (33, (("A", 5, 1), ("B", 10, 1)))]
            for symbol, price, score in entries]
    result = run(tmp_path, rows, max_symbol_weight=.5, top_k=2, drawdown_pause=.1)
    assert [f["instrument_id"] for f in table(tmp_path, "fills")] == ["A"]
    assert any(o["reason"] == "drawdown_pause" for o in table(tmp_path, "orders"))
    assert result["metrics"]["minute_max_drawdown"] == pytest.approx(.25)


def test_multiple_intraday_orders_have_distinct_timestamp_ids(tmp_path):
    run(tmp_path, [bar(), bar(minute=32, score=-1), bar(minute=33),
                   bar(minute=34, score=-1), bar(minute=35)], settlement_days=0)
    fills = table(tmp_path, "fills")
    assert [f["side"] for f in fills] == ["buy", "sell", "buy", "sell"]
    assert len({f["order_id"] for f in fills}) == 4
    assert len({f["timestamp"] for f in fills}) == 4


def test_daily_sharpe_differs_from_minute_returns(tmp_path):
    rows = [bar(), bar(minute=32, close=20), bar(day=8, price=20, close=10),
            bar(day=8, minute=32, price=10, close=12)]
    result = run(tmp_path, rows)
    # Daily returns are +100%, -40%; sample standard deviation = sqrt(.98).
    assert result["metrics"]["sharpe"] == pytest.approx(.3 / (.98 ** .5) * (252 ** .5))
    assert result["metrics"]["max_drawdown"] == pytest.approx(.4)
    assert result["metrics"]["minute_max_drawdown"] == pytest.approx(.5)


def test_instrument_overrides_preserve_lots_and_t0_settlement(tmp_path):
    run(tmp_path, [bar(), bar(minute=32, score=-1), bar(minute=33)],
        lot_size=100, instruments={"A": {"lot_size": 10, "settlement_days": 0}}, initial_cash=950)
    assert [(f["side"], int(f["quantity"])) for f in table(tmp_path, "fills")] == [("buy", 90), ("sell", 90)]


def test_cancellation_during_stream_never_writes_success_summary(tmp_path):
    from quanto.learning.intraday import run_intraday_backtest

    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 5

    with pytest.raises(Exception, match="cancelled"):
        run_intraday_backtest(iter([bar(), bar(minute=32), bar(minute=33)]),
                             dict(start="2024-01-05T00:00:00Z", end="2024-01-06T00:00:00Z"),
                             tmp_path, lambda event: None, cancelled, {})
    assert not (tmp_path / "summary.json").exists()


def test_buy_cost_cannot_push_existing_holding_above_symbol_cap(tmp_path):
    rows = [bar(minute=minute, symbol=symbol) for minute in (31, 32) for symbol in ("A", "B")]
    result = run(tmp_path, rows, top_k=2, max_symbol_weight=.5, minimum_commission=150,
                 settlement_days=0)
    assert [(f["instrument_id"], int(f["quantity"])) for f in table(tmp_path, "fills")] == [("A", 42)]
    assert result["metrics"]["final_equity"] == 850
    assert any(o["instrument_id"] == "B" and o["reason"] == "weight_cap" for o in table(tmp_path, "orders"))
    assert all(float(p["weight"]) <= .5 for p in table(tmp_path, "positions"))


def test_percentage_cost_limits_new_buy_to_keep_other_holding_below_cap(tmp_path):
    rows = [bar(minute=minute, symbol=symbol) for minute in (31, 32) for symbol in ("A", "B")]
    result = run(tmp_path, rows, top_k=2, max_symbol_weight=.5, commission_rate=.1,
                 settlement_days=0)
    # A costs 517 for 47 shares; B's fee can consume only the remaining 13 of equity headroom.
    assert [(f["instrument_id"], int(f["quantity"])) for f in table(tmp_path, "fills")] == [("A", 47), ("B", 13)]
    assert result["metrics"]["final_equity"] == 940
    assert all(float(p["weight"]) <= .5 for p in table(tmp_path, "positions"))


def test_fill_cost_pause_blocks_other_pending_buys_at_same_open(tmp_path):
    rows = [bar(minute=minute, symbol=symbol) for minute in (31, 32) for symbol in ("A", "B")]
    result = run(tmp_path, rows, top_k=2, max_symbol_weight=.5, minimum_commission=150,
                 settlement_days=0, drawdown_pause=.1)
    assert [f["instrument_id"] for f in table(tmp_path, "fills")] == ["A"]
    assert any(o["instrument_id"] == "B" and o["reason"] == "drawdown_pause"
               for o in table(tmp_path, "orders"))
    assert result["metrics"]["minute_max_drawdown"] == pytest.approx(.15)


def test_risk_liquidation_runs_between_scheduled_rebalances(tmp_path):
    result = run(tmp_path, [bar(), bar(minute=32, close=8), bar(minute=33, price=8)],
                 rebalance_every=10, drawdown_pause=.1, settlement_days=0)
    assert [(f["side"], int(f["quantity"])) for f in table(tmp_path, "fills")] == [("buy", 100), ("sell", 100)]
    assert table(tmp_path, "fills")[-1]["signal_time"] == "2024-01-05T01:32:00+00:00"
    assert result["metrics"]["final_cash"] == 800


def test_missing_complete_calendar_sessions_are_rejected_without_summary(tmp_path):
    with pytest.raises(ValueError, match="missing.*trading.*day|Missing.*trading.*day"):
        run(tmp_path, [bar(), bar(day=10)], baseline="cash", cash_rate=.252,
            metadata={"trading_days": ["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10"]})
    assert not (tmp_path / "summary.json").exists()


@pytest.mark.parametrize("rows, changes", [([], {}), ([bar(day=8)], dict(end="2024-01-06T00:00:00Z")),
                                          ([bar()], dict(start="2024-01-06T00:00:00Z"))])
def test_empty_backtest_interval_is_rejected_without_summary(tmp_path, rows, changes):
    with pytest.raises(ValueError, match="No.*bars|no.*bars|empty"):
        run(tmp_path, rows, **changes)
    assert not (tmp_path / "summary.json").exists()


@pytest.mark.parametrize("rows, missing_day", [([bar(day=8)], "2024-01-05"),
                                             ([bar()], "2024-01-08"), ([], "2024-01-05.*2024-01-08")])
def test_expected_calendar_rejects_missing_first_last_or_all_days(tmp_path, rows, missing_day):
    with pytest.raises(ValueError, match=missing_day):
        run(tmp_path, rows, baseline="cash", cash_rate=.252,
            metadata={"trading_days": ["2024-01-05", "2024-01-08"],
                      "expected_trading_days": ["2024-01-05", "2024-01-08"]})
    assert not (tmp_path / "summary.json").exists()


def test_expected_days_use_partial_requested_session_not_entire_dataset_calendar(tmp_path):
    result = run(tmp_path, [bar(day=8), bar(day=8, minute=32)], baseline="cash", cash_rate=.252,
                 start="2024-01-08T01:31:00Z", end="2024-01-08T01:32:00Z",
                 metadata={"trading_days": ["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10"],
                           "expected_trading_days": ["2024-01-08"]})
    assert result["counts"]["bars"] == 1
    assert result["counts"]["trading_days"] == 1
    assert result["metrics"]["cash_interest"] == 1
    assert "coverage" in result["assumptions"]


def test_without_expected_days_reports_unknown_endpoint_coverage(tmp_path):
    result = run(tmp_path, [bar(day=8)], baseline="cash",
                 metadata={"trading_days": ["2024-01-05", "2024-01-08", "2024-01-09"]})
    assert "cannot confirm" in result["assumptions"]["coverage"]


def test_observed_date_outside_expected_days_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unexpected.*2024-01-08"):
        run(tmp_path, [bar(day=8)], metadata={"expected_trading_days": ["2024-01-05"]})
    assert not (tmp_path / "summary.json").exists()


@pytest.mark.parametrize("expected", [["2024-01-08", "2024-01-05"],
                                      ["2024-01-05", "2024-01-05"], ["invalid"], "2024-01-05"])
def test_expected_trading_days_requires_ordered_unique_iso_dates(tmp_path, expected):
    with pytest.raises(ValueError, match="expected_trading_days"):
        run(tmp_path, [bar()], metadata={"expected_trading_days": expected})
