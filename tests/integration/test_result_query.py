import csv
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Lock

import pytest
from fastapi.testclient import TestClient

from quanto.api.app import create_app
from quanto.workbench import service as service_module
from quanto.workbench import result_index as result_index_module
from quanto.workbench.result_index import ResultIndex
from quanto.workbench.service import WorkbenchService
from tests.integration.test_worker_snapshot import ready_run, run_until_finished


def _replace_tables(root, tables):
    for name, rows in tables.items():
        path = root / f"{name}.csv"
        with path.open(newline="", encoding="utf-8") as stream:
            columns = next(csv.reader(stream))
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.iterdir() if path.name != "manifest.json"}
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"] = hashes
    (root / "manifest.json").write_text(json.dumps(manifest))
    hashes["manifest.json"] = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    receipt_path = root.parent / "control" / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["result"]["hashes"] = hashes
    receipt_path.write_text(json.dumps(receipt))


def test_completed_index_persists_statistics_for_selective_symbol_queries(tmp_path):
    final = tmp_path / "final"
    final.mkdir()
    path = final / "positions.csv"
    start = date(2020, 1, 2)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["date", "symbol", "shares"])
        writer.writeheader()
        for n in range(41634):
            writer.writerow({"date": (start + timedelta(days=n // 18)).isoformat(),
                             "symbol": "159611" if n % 100 == 0 else f"{100000 + n % 20:06d}",
                             "shares": n})
    index = ResultIndex(tmp_path, "selective-run", {"positions.csv": hashlib.sha256(path.read_bytes()).hexdigest()})
    index.ensure(final, lambda: None)
    with sqlite3.connect(index.path) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT payload FROM rows WHERE table_name=? AND symbol=? "
                          "AND date>=? AND date<=? ORDER BY date,seq LIMIT 100",
                          ("positions", "159611", "2020-01-02", "2026-05-19")).fetchall()
        assert any("rows_symbol_date" in row[3] for row in plan), plan
        assert db.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0] > 0


def test_indexed_table_pages_filter_and_survive_restart_without_csv_parse(tmp_path, monkeypatch):
    store, run, _ = ready_run(tmp_path)
    assert run_until_finished(store, run.job_id).state == "succeeded"
    root = store.artifact_root / run.job_id / "final"
    _replace_tables(root, {"positions": [
        {"date": "2024-01-01" if n < 15000 else "2024-01-02", "symbol": "A" if n % 2 else "B",
         "shares": n, "sellable_shares": n, "market_value": n, "weight": 0.5,
         "cost_basis": n, "unrealized_pnl": 0, "realized_pnl": 0}
        for n in range(30000)]})
    service = WorkbenchService(store.root)
    parses = 0
    parse_lock = Lock()
    original_reader = csv.DictReader

    def count_parse(*args, **kwargs):
        nonlocal parses
        with parse_lock:
            parses += 1
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(csv, "DictReader", count_parse)
    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = list(pool.map(lambda _: service.table(run.run_id, "positions", limit=100,
                                                      date_from="2024-01-02", date_to="2024-01-02", symbol="A"), range(6)))
    assert all(page.total == 7500 and len(page.items) == 100 and page.next_cursor == "100" for page in pages)
    assert parses == 12
    tail = service.table(run.run_id, "positions", cursor="7499", limit=100, date_from="2024-01-02", date_to="2024-01-02", symbol="A")
    assert len(tail.items) == 1 and tail.next_cursor is None
    assert service.table(run.run_id, "positions", cursor="7500", date_from="2024-01-02", date_to="2024-01-02", symbol="A").items == []
    receipt = json.loads((root.parent / "control" / "receipt.json").read_text(encoding="utf-8"))
    index_file = ResultIndex(store.root, run.run_id, receipt["result"]["hashes"]).path
    with sqlite3.connect(index_file) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT payload FROM rows WHERE table_name=? AND symbol=? AND date>=? AND date<=? ORDER BY date,seq LIMIT 100",
                          ("positions", "A", "2024-01-02", "2024-01-02")).fetchall()
    assert any("rows_date" in row[3] or "rows_symbol_date" in row[3] for row in plan)

    def fail_parse(*args, **kwargs):
        raise AssertionError("a published index must not parse CSV again")

    monkeypatch.setattr(csv, "DictReader", fail_parse)
    restarted = WorkbenchService(store.root)
    assert restarted.table(run.run_id, "positions", limit=1).total == 30000
    original = (root / "positions.csv").read_bytes()
    stat = (root / "positions.csv").stat()
    (root / "positions.csv").write_bytes(original.replace(b"2024", b"2025", 1))
    os.utime(root / "positions.csv", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(ValueError, match="INVALID_OUTPUT"):
        restarted.table(run.run_id, "positions", limit=1)


def test_context_date_follows_cross_day_order_links_and_keeps_global_events(tmp_path):
    store, run, _ = ready_run(tmp_path)
    assert run_until_finished(store, run.job_id).state == "succeeded"
    root = store.artifact_root / run.job_id / "final"
    _replace_tables(root, {
        "orders": [
            {"id": "a", "symbol": "A", "signal_date": "2024-01-01", "execution_date": "2024-01-02"},
            {"id": "b", "symbol": "B", "signal_date": "2023-12-31", "execution_date": "2024-01-02"}],
        "fills": [
            {"order_id": "a", "symbol": "A", "trade_date": "2024-01-02"},
            {"order_id": "b", "symbol": "B", "trade_date": "2024-01-02"}],
        "signals": [{"date": day, "intent": day} for day in ("2023-12-31", "2024-01-01", "2024-01-02")],
        "risk_events": [{"date": day, "code": day} for day in ("2023-12-31", "2024-01-01", "2024-01-02")],
        "matched_trades": [{"symbol": "A", "entry_date": "2024-01-01", "exit_date": "2024-01-02", "quantity": 1}],
        "open_lots": [{"symbol": "A", "entry_date": "2024-01-01", "quantity": 1}],
    })
    service = WorkbenchService(store.root)
    assert [row["id"] for row in service.table(run.run_id, "orders", context_date="2024-01-02", symbol="A").items] == ["a"]
    assert [row["order_id"] for row in service.table(run.run_id, "fills", context_date="2024-01-02", symbol="A").items] == ["a"]
    assert [row["date"] for row in service.table(run.run_id, "signals", context_date="2024-01-02", symbol="A").items] == ["2024-01-01", "2024-01-02"]
    assert [row["date"] for row in service.table(run.run_id, "risk_events", context_date="2024-01-02", symbol="A").items] == ["2024-01-01", "2024-01-02"]
    assert service.table(run.run_id, "orders", context_date="2024-01-04").total == 0
    assert service.table(run.run_id, "matched_trades", date_from="2024-01-02", date_to="2024-01-02").total == 1
    assert service.table(run.run_id, "open_lots", date_from="2024-01-01", date_to="2024-01-01").total == 1


def test_chart_endpoints_preserve_extremes_and_bound_allocation(tmp_path):
    store, run, _ = ready_run(tmp_path)
    assert run_until_finished(store, run.job_id).state == "succeeded"
    root = store.artifact_root / run.job_id / "final"
    days = [f"2024-01-{day:02d}" for day in range(1, 31)]
    _replace_tables(root, {
        "curves": [{"date": day, "equity": 100 + n, "return": 0, "drawdown": -0.9 if n == 13 else 0,
                    "benchmark_equity": 999 if n == 17 else 100, "benchmark_return": 0}
                   for n, day in enumerate(days)],
        "positions": [{"date": day, "symbol": symbol, "shares": 1, "sellable_shares": 1,
                       "market_value": 50, "weight": 0.5, "cost_basis": 50,
                       "unrealized_pnl": 0, "realized_pnl": 0}
                      for day in days for symbol in ("A", "B")] +
                     [{"date": days[0], "symbol": "A", "shares": 1, "sellable_shares": 1,
                       "market_value": 10, "weight": 0.1, "cost_basis": 10,
                       "unrealized_pnl": 0, "realized_pnl": 0}],
        "monthly_returns": [{"period": "2023-12", "return": 0.1}, {"period": "2024-01", "return": 0.2}],
        "annual_returns": [{"period": "2023", "return": 0.1}, {"period": "2024", "return": 0.2}],
    })
    app = create_app(store.root)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"Authorization": f"Bearer {client.get('/api/session').json()['token']}"}
    curves = client.get(f"/api/runs/{run.run_id}/charts/curves?max_points=8", headers=headers).json()
    assert curves["total_rows"] == 30 and curves["sampled"] is True
    assert len(curves["items"]) <= 8
    assert curves["items"][0]["date"] == days[0] and curves["items"][-1]["date"] == days[-1]
    assert -0.9 in [row["drawdown"] for row in curves["items"]]
    assert 999 in [row["benchmark_equity"] for row in curves["items"]]
    endpoints = client.get(f"/api/runs/{run.run_id}/charts/curves?max_points=2", headers=headers).json()
    assert [row["date"] for row in endpoints["items"]] == [days[0], days[-1]]
    allocation = client.get(f"/api/runs/{run.run_id}/charts/allocation?max_points=9", headers=headers).json()
    assert allocation["total_rows"] == 61 and allocation["sampled"] is True
    assert len(allocation["items"]) <= 9
    assert allocation["items"][0]["date"] == days[0] and allocation["items"][-1]["date"] == days[-1]
    selected_dates = {row["date"] for row in allocation["items"]}
    assert all({row["symbol"] for row in allocation["items"] if row["date"] == day} == {"A", "B"}
               for day in selected_dates)
    assert sum(row["weight"] for row in allocation["items"] if row["date"] == days[0]) == 1.1
    assert all(sum(row["weight"] for row in allocation["items"] if row["date"] == day) == 1.0
               for day in selected_dates - {days[0]})
    assert client.get(f"/api/runs/{run.run_id}/charts/allocation?max_points=3", headers=headers).status_code == 422
    monthly = client.get(f"/api/runs/{run.run_id}/charts/monthly_returns?date_from=2024-01-15&date_to=2024-01-31", headers=headers).json()
    annual = client.get(f"/api/runs/{run.run_id}/charts/annual_returns?date_from=2024-10-01&date_to=2024-12-31", headers=headers).json()
    assert [row["period"] for row in monthly["items"]] == ["2024-01"]
    assert [row["period"] for row in annual["items"]] == ["2024"]
    receipt = json.loads((root.parent / "control" / "receipt.json").read_text(encoding="utf-8"))
    index = ResultIndex(store.root, run.run_id, receipt["result"]["hashes"])
    for points in range(2, 72):
        index.chart("curves", None, None, points)
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT count(*) FROM chart_cache").fetchone()[0] <= 64
        db.execute("UPDATE chart_cache SET value='broken JSON'")
        db.commit()
    repaired = client.get(f"/api/runs/{run.run_id}/charts/curves?max_points=8", headers=headers).json()
    assert repaired == curves
    with sqlite3.connect(index.path) as db:
        assert json.loads(db.execute("SELECT value FROM chart_cache WHERE cache_key=?",
                                     (json.dumps(["curves", None, None, 8]),)).fetchone()[0]) == curves
    restricted = client.get(f"/api/runs/{run.run_id}/tables/positions?date_from=2024-01-02&date_to=2024-01-02&symbol=A", headers=headers).json()
    assert restricted["total"] == 1 and restricted["items"][0]["symbol"] == "A"
    assert client.get(f"/api/runs/{run.run_id}/charts/curves?max_points=1", headers=headers).status_code == 422
    logs = client.get(f"/api/runs/{run.run_id}/logs?tail=true&limit=2", headers=headers).json()
    assert len(logs["items"]) == 2
    assert logs["items"][-1]["seq"] > logs["items"][0]["seq"]


def test_results_and_allocation_use_positive_shares_from_complete_positions(tmp_path):
    store, run, _ = ready_run(tmp_path)
    service = WorkbenchService(store.root)
    assert run_until_finished(store, run.job_id).state == "succeeded"
    root = store.artifact_root / run.job_id / "final"
    days = [f"2024-01-{day:02d}" for day in range(1, 10)]
    _replace_tables(root, {"positions": [
        {"date": day, "symbol": symbol,
         "shares": 1 if (symbol == "A" and day == days[0]) or (symbol == "C" and day == days[4]) else 0,
         "sellable_shares": 0, "market_value": 10 if symbol == "A" and day == days[0] else 0,
         "weight": 0.5 if symbol == "B" else 0, "cost_basis": 0,
         "unrealized_pnl": 0, "realized_pnl": 0}
        for day in days for symbol in ("C", "B", "A")]})

    allocation = service.chart(run.run_id, "allocation", max_points=4)
    assert allocation["total_rows"] == 18
    assert allocation["sampled"] is True
    assert len(allocation["items"]) <= 4
    assert [row["date"] for row in allocation["items"]] == [days[0], days[0], days[-1], days[-1]]
    assert {row["symbol"] for row in allocation["items"]} == {"A", "C"}
    assert next(row for row in allocation["items"] if row["date"] == days[-1] and row["symbol"] == "A")["weight"] == 0
    assert service.chart(run.run_id, "allocation", days[1], days[3], 2)["items"] == []
    assert service.results(run.run_id)["held_symbols"] == ["A", "C"]


def test_unfinished_result_has_unknown_held_symbols(tmp_path):
    store, run, _ = ready_run(tmp_path)
    assert WorkbenchService(store.root).results(run.run_id)["held_symbols"] is None


def test_all_cash_allocation_and_summary_are_empty(tmp_path):
    store, run, _ = ready_run(tmp_path)
    assert run_until_finished(store, run.job_id).state == "succeeded"
    root = store.artifact_root / run.job_id / "final"
    _replace_tables(root, {"positions": [
        {"date": day, "symbol": "A", "shares": 0, "sellable_shares": 0,
         "market_value": 0, "weight": 0, "cost_basis": 0,
         "unrealized_pnl": 0, "realized_pnl": 0}
        for day in ("2024-01-01", "2024-01-02")]})
    service = WorkbenchService(store.root)
    assert service.chart(run.run_id, "allocation", max_points=2) == {
        "items": [], "total_rows": 0, "sampled": False}
    assert service.results(run.run_id)["held_symbols"] == []


def test_allocation_rebuilds_legacy_chart_cache(tmp_path):
    store, run, _ = ready_run(tmp_path)
    assert run_until_finished(store, run.job_id).state == "succeeded"
    root = store.artifact_root / run.job_id / "final"
    _replace_tables(root, {"positions": [
        {"date": "2024-01-01", "symbol": symbol, "shares": shares,
         "sellable_shares": shares, "market_value": shares, "weight": shares,
         "cost_basis": 0, "unrealized_pnl": 0, "realized_pnl": 0}
        for symbol, shares in (("A", 1), ("B", 0))]})
    receipt = json.loads((root.parent / "control" / "receipt.json").read_text(encoding="utf-8"))
    index = ResultIndex(store.root, run.run_id, receipt["result"]["hashes"])
    index.ensure(root, lambda: None)
    with closing(sqlite3.connect(index.path)) as db, db:
        db.execute("UPDATE meta SET value='3' WHERE key='version'")
        db.execute("INSERT INTO chart_cache VALUES (?,?,1)", (
            json.dumps(["allocation", None, None, 2]),
            json.dumps({"items": [{"date": "2024-01-01", "symbol": "B", "weight": 0}],
                        "total_rows": 2, "sampled": False})))
    chart = WorkbenchService(store.root).chart(run.run_id, "allocation", max_points=2)
    assert [row["symbol"] for row in chart["items"]] == ["A"]
    assert chart["total_rows"] == 1


def test_partial_table_filters_without_creating_final_index(tmp_path):
    store, run, _ = ready_run(tmp_path)
    control = store.artifact_root / run.job_id / "control"
    control.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(control / "partial.sqlite3") as db:
        db.executescript("CREATE TABLE rows(table_name TEXT,row_key TEXT,payload TEXT);"
                         "CREATE TABLE progress(singleton INTEGER,completed_through TEXT);")
        db.execute("INSERT INTO progress VALUES (1,'2024-01-02')")
        for index, row in enumerate((
            {"date": "2024-01-01", "symbol": "A", "shares": 1},
            {"date": "2024-01-02", "symbol": "A", "shares": 2},
            {"date": "2024-01-02", "symbol": "B", "shares": 3},
        )):
            db.execute("INSERT INTO rows VALUES (?,?,?)", ("positions", str(index), json.dumps(row)))
    service = WorkbenchService(store.root)
    page = service.table(run.run_id, "positions", date_from="2024-01-02", symbol="A", limit=1)
    assert page.total == 1 and page.items[0]["shares"] == 2 and page.next_cursor == "1"
    assert service.table(run.run_id, "positions", cursor="1", date_from="2024-01-02", symbol="A").items == []
    assert not (store.root / "result-indexes").exists()


def test_large_partial_table_uses_sql_page_and_updates_orders_in_place(tmp_path, monkeypatch):
    store, run, _ = ready_run(tmp_path)
    control = store.artifact_root / run.job_id / "control"
    control.mkdir(parents=True, exist_ok=True)
    partial = control / "partial.sqlite3"
    with sqlite3.connect(partial) as db:
        db.executescript("CREATE TABLE rows(table_name TEXT,row_key TEXT,payload TEXT,PRIMARY KEY(table_name,row_key));"
                         "CREATE TABLE progress(singleton INTEGER PRIMARY KEY,completed_through TEXT);")
        db.executemany("INSERT INTO rows VALUES (?,?,?)", (
            ("positions", str(index), json.dumps({"date": "2024-01-01" if index < 15000 else "2024-01-02",
                                                    "symbol": "A" if index % 2 else "B", "shares": index}))
            for index in range(30000)))
        db.execute("INSERT INTO rows VALUES (?,?,?)", ("orders", "one", json.dumps(
            {"id": "one", "symbol": "A", "signal_date": "2024-01-01", "execution_date": "2024-01-02"})))
        db.execute("INSERT INTO rows VALUES (?,?,?)", ("fills", "fill", json.dumps(
            {"order_id": "one", "symbol": "A", "trade_date": "2024-01-02"})))
        for day in ("2024-01-01", "2024-01-02"):
            db.execute("INSERT INTO rows VALUES (?,?,?)", ("signals", day, json.dumps({"date": day})))
            db.execute("INSERT INTO rows VALUES (?,?,?)", ("risk_events", day, json.dumps({"date": day})))
    monkeypatch.setattr(service_module, "read_partial", lambda _: (_ for _ in ()).throw(AssertionError("full partial read")))
    service = WorkbenchService(store.root)
    parses = 0
    original_loads = json.loads

    def count_parse(*args, **kwargs):
        nonlocal parses
        parses += 1
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(result_index_module.json, "loads", count_parse)
    page = service.table(run.run_id, "positions", limit=100, date_from="2024-01-02", date_to="2024-01-02", symbol="A")
    assert page.total == 7500 and len(page.items) == 100 and page.next_cursor == "100"
    assert parses <= 100
    with sqlite3.connect(partial) as db:
        indices = {row[1] for row in db.execute("PRAGMA index_list(rows)")}
        assert "partial_symbol_date" in indices
        plan = db.execute("EXPLAIN QUERY PLAN SELECT payload FROM rows WHERE table_name=? AND json_extract(payload,'$.symbol')=? AND substr(coalesce(nullif(json_extract(payload,'$.date'),''),nullif(json_extract(payload,'$.period'),''),nullif(json_extract(payload,'$.signal_date'),''),nullif(json_extract(payload,'$.trade_date'),''),nullif(json_extract(payload,'$.exit_date'),''),nullif(json_extract(payload,'$.entry_date'),'')),1,10)>=? LIMIT 100",
                          ("positions", "A", "2024-01-02")).fetchall()
        assert any("partial_symbol_date" in row[3] for row in plan)
        db.execute("INSERT INTO rows VALUES (?,?,?) ON CONFLICT(table_name,row_key) DO UPDATE SET payload=excluded.payload",
                   ("orders", "one", json.dumps({"id": "one", "symbol": "A", "signal_date": "2024-01-01",
                                                      "execution_date": "2024-01-02", "status": "filled"})))
    orders = service.table(run.run_id, "orders", context_date="2024-01-02", symbol="A")
    assert orders.total == 1 and orders.items[0]["status"] == "filled"
    assert service.table(run.run_id, "fills", context_date="2024-01-02", symbol="A").items[0]["order_id"] == "one"
    assert [row["date"] for row in service.table(run.run_id, "signals", context_date="2024-01-02", symbol="A").items] == ["2024-01-01", "2024-01-02"]
    assert [row["date"] for row in service.table(run.run_id, "risk_events", context_date="2024-01-02", symbol="A").items] == ["2024-01-01", "2024-01-02"]


def test_log_tail_reads_only_recent_complete_utf8_lines(tmp_path, monkeypatch):
    store, run, _ = ready_run(tmp_path)
    control = store.artifact_root / run.job_id / "control"
    control.mkdir(parents=True, exist_ok=True)
    path = control / "events.ndjson"
    with path.open("wb") as stream:
        for seq in range(1, 10001):
            value = {"seq": seq, "code": "成交", "payload": {"tables": {"orders": [{"id": str(seq)}]}, "completed_through": "2024-01-02"}}
            stream.write(json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n")
        stream.write(b'{"seq":10001,"code":"unfinished"')
    service = WorkbenchService(store.root)
    parses = 0
    original_loads = json.loads

    def count_parse(*args, **kwargs):
        nonlocal parses
        parses += 1
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(service_module.json, "loads", count_parse)
    page = service.logs(run.job_id, None, limit=100, tail=True)
    assert [row["seq"] for row in page.items] == list(range(9901, 10001))
    assert page.items[-1]["code"] == "成交"
    assert page.items[-1]["payload"] == {"table_counts": {"orders": 1}, "completed_through": "2024-01-02"}
    assert parses <= 100
