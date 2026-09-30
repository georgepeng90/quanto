"""Rebuildable SQLite query copy of verified, frozen result artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4


INDEX_VERSION = "3"
CHART_CACHE_LIMIT = 64
PARTIAL_COLUMNS = {
    "date": "substr(coalesce(nullif(json_extract(payload,'$.date'),''),nullif(json_extract(payload,'$.period'),''),"
            "nullif(json_extract(payload,'$.signal_date'),''),nullif(json_extract(payload,'$.trade_date'),''),"
            "nullif(json_extract(payload,'$.exit_date'),''),nullif(json_extract(payload,'$.entry_date'),'')),1,10)",
    "symbol": "json_extract(payload,'$.symbol')",
    "order_id": "coalesce(json_extract(payload,'$.id'),json_extract(payload,'$.order_id'))",
    "signal_date": "substr(json_extract(payload,'$.signal_date'),1,10)",
    "execution_date": "substr(json_extract(payload,'$.execution_date'),1,10)",
    "trade_date": "substr(json_extract(payload,'$.trade_date'),1,10)",
}


class ResultIndex:
    def __init__(self, workspace: Path, run_id: str, hashes: dict[str, str]):
        directory = workspace / "result-indexes"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{hashlib.sha256(run_id.encode()).hexdigest()}.sqlite"
        self.lock_path = directory / ".build-lock.sqlite"
        self.fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()

    def _is_current(self) -> bool:
        if not self.path.is_file():
            return False
        try:
            with closing(sqlite3.connect(self.path)) as db:
                current = db.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone() == (self.fingerprint,) and db.execute(
                    "SELECT value FROM meta WHERE key='version'").fetchone() == (INDEX_VERSION,)
                if current:
                    db.execute("SELECT 1 FROM rows LIMIT 1").fetchone()
                    db.execute("SELECT 1 FROM chart_cache LIMIT 1").fetchone()
                return current
        except sqlite3.DatabaseError:
            return False

    def ensure(self, final: Path, validate) -> None:
        if self._is_current():
            return
        with closing(sqlite3.connect(self.lock_path, timeout=120)) as lock, lock:
            lock.execute("BEGIN IMMEDIATE")
            if self._is_current():
                return
            validate()
            from quanto.workbench.service import _typed_row
            temporary = self.path.with_name(f"{self.path.stem}-{uuid4().hex}.tmp")
            try:
                with closing(sqlite3.connect(temporary)) as db, db:
                    db.executescript("""
                        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                        CREATE TABLE rows (
                            table_name TEXT NOT NULL, seq INTEGER NOT NULL, date TEXT,
                            symbol TEXT, order_id TEXT, signal_date TEXT, execution_date TEXT,
                            trade_date TEXT, payload TEXT NOT NULL,
                            PRIMARY KEY (table_name, seq));
                        CREATE TABLE chart_cache (cache_key TEXT PRIMARY KEY, value TEXT NOT NULL, touched INTEGER NOT NULL);
                    """)
                    for path in sorted(final.glob("*.csv")):
                        with path.open(encoding="utf-8", newline="") as stream:
                            reader = csv.DictReader(stream)
                            batch = []
                            for seq, row in enumerate(reader):
                                typed = _typed_row(row)
                                primary_date = (row.get("date") or row.get("period") or row.get("signal_date") or
                                                row.get("trade_date") or row.get("exit_date") or row.get("entry_date"))
                                batch.append((path.stem, seq, primary_date[:10] if primary_date else None,
                                              row.get("symbol"), row.get("id") if path.stem == "orders" else row.get("order_id"),
                                              (row.get("signal_date") or "")[:10] or None,
                                              (row.get("execution_date") or "")[:10] or None,
                                              (row.get("trade_date") or "")[:10] or None,
                                              json.dumps(typed, ensure_ascii=False, allow_nan=False)))
                                if len(batch) == 1000:
                                    db.executemany("INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?)", batch)
                                    batch.clear()
                            if batch:
                                db.executemany("INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?)", batch)
                    db.executescript("""
                        CREATE INDEX rows_date ON rows(table_name,date,seq);
                        CREATE INDEX rows_symbol_date ON rows(table_name,symbol,date,seq);
                        CREATE INDEX rows_signal_date ON rows(table_name,signal_date,seq);
                        CREATE INDEX rows_execution_date ON rows(table_name,execution_date,seq);
                        CREATE INDEX rows_order_id ON rows(table_name,order_id,seq);
                    """)
                    db.execute("ANALYZE")
                    db.executemany("INSERT INTO meta VALUES (?,?)", (("fingerprint", self.fingerprint), ("version", INDEX_VERSION)))
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _where(name: str, date_from: str | None, date_to: str | None, symbol: str | None,
               context_date: str | None, columns: dict[str, str] | None = None) -> tuple[str, list]:
        column = lambda key: columns[key] if columns else key
        clauses = ["table_name=?"]
        params = [name]
        if context_date:
            order_filter = f"table_name='orders' AND ({column('signal_date')}=? OR {column('execution_date')}=?)"
            order_params = [context_date, context_date]
            if symbol:
                order_filter += f" AND {column('symbol')}=?"
                order_params.append(symbol)
            if name == "orders":
                clauses.append(f"({column('signal_date')}=? OR {column('execution_date')}=?)")
                params.extend((context_date, context_date))
            elif name == "fills":
                clauses.append(f"({column('trade_date')}=? OR {column('order_id')} IN (SELECT {column('order_id')} FROM rows WHERE {order_filter}))")
                params.extend([context_date, *order_params])
            elif name in {"signals", "risk_events"}:
                clauses.append(f"({column('date')}=? OR {column('date')} IN (SELECT {column('signal_date')} FROM rows WHERE {order_filter}))")
                params.extend([context_date, *order_params])
            else:
                clauses.append(f"{column('date')}=?")
                params.append(context_date)
        else:
            period_length = 7 if name == "monthly_returns" else 4 if name == "annual_returns" else 10
            if date_from:
                clauses.append(f"{column('date')}>=?")
                params.append(date_from[:period_length])
            if date_to:
                clauses.append(f"{column('date')}<=?")
                params.append(date_to[:period_length])
        if symbol and name not in {"signals", "risk_events"}:
            clauses.append(f"{column('symbol')}=?")
            params.append(symbol)
        return " AND ".join(clauses), params

    def page(self, name: str, offset: int, limit: int, date_from: str | None, date_to: str | None,
             symbol: str | None, context_date: str | None) -> tuple[list[dict], int]:
        where, params = self._where(name, date_from, date_to, symbol, context_date)
        with closing(sqlite3.connect(self.path)) as db:
            total = db.execute(f"SELECT count(*) FROM rows WHERE {where}", params).fetchone()[0]
            order = "date,seq" if date_from or date_to or symbol or context_date else "seq"
            items = [json.loads(row[0]) for row in db.execute(
                f"SELECT payload FROM rows WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset))]
        return items, total

    def chart(self, name: str, date_from: str | None, date_to: str | None, max_points: int) -> dict:
        key = json.dumps([name, date_from, date_to, max_points])
        with closing(sqlite3.connect(self.path)) as db, db:
            cached = db.execute("SELECT value FROM chart_cache WHERE cache_key=?", (key,)).fetchone()
            if cached:
                try:
                    result = json.loads(cached[0])
                except (json.JSONDecodeError, TypeError):
                    db.execute("DELETE FROM chart_cache WHERE cache_key=?", (key,))
                else:
                    db.execute("UPDATE chart_cache SET touched=(SELECT coalesce(max(touched),0)+1 FROM chart_cache) WHERE cache_key=?", (key,))
                    return result
            source = "positions" if name == "allocation" else name
            where, params = self._where(source, date_from, date_to, None, None)
            source_total = db.execute(f"SELECT count(*) FROM rows WHERE {where}", params).fetchone()[0]
            if name == "allocation":
                days = list(db.execute(f"SELECT date,count(DISTINCT symbol) FROM rows WHERE {where} GROUP BY date ORDER BY date", params))
                budget = max_points // max((count for _, count in days), default=1)
                if len(days) > 1 and budget < 2 or len(days) == 1 and budget < 1:
                    raise ValueError("max_points cannot fit complete allocation endpoint dates")
                if len(days) <= budget:
                    selected_days = [day for day, _ in days]
                else:
                    selected_days = [days[round(index * (len(days) - 1) / (budget - 1))][0]
                                     for index in range(budget)]
                items = []
                if selected_days:
                    placeholders = ",".join("?" for _ in selected_days)
                    query = ("SELECT date,symbol,sum(CAST(json_extract(payload,'$.market_value') AS REAL)),"
                             "sum(CAST(json_extract(payload,'$.weight') AS REAL)) FROM rows "
                             f"WHERE {where} AND date IN ({placeholders}) GROUP BY date,symbol ORDER BY date,symbol")
                    items = [{"date": row[0], "symbol": row[1], "market_value": row[2], "weight": row[3]}
                             for row in db.execute(query, (*params, *selected_days))]
            else:
                values = (json.loads(row[0]) for row in db.execute(
                    f"SELECT payload FROM rows WHERE {where} ORDER BY seq", params))
                # Keep a bounded set of extrema per bucket while streaming the query.
                total = source_total
                if total <= max_points:
                    items = list(values)
                else:
                    buckets = max(1, (max_points - 2) // (6 if name == "curves" else 1))
                    selected = {}
                    first = last = None
                    for index, item in enumerate(values):
                        if index == 0:
                            first = item
                        last = item
                        bucket = min(buckets - 1, index * buckets // total)
                        if name == "curves":
                            for field in ("equity", "benchmark_equity", "drawdown"):
                                value = item.get(field)
                                if value is None:
                                    continue
                                for direction in (1, -1):
                                    slot = (bucket, field, direction)
                                    existing = selected.get(slot)
                                    if existing is None or direction * value < direction * existing[1].get(field):
                                        selected[slot] = (index, item)
                        else:
                            selected.setdefault((bucket,), (index, item))
                    picks = {index: item for index, item in selected.values()}
                    picks[0] = first
                    picks[total - 1] = last
                    if len(picks) > max_points:
                        fields = ("equity", "benchmark_equity", "drawdown")
                        spans = {field: max(item.get(field) or 0 for item in picks.values()) -
                                 min(item.get(field) or 0 for item in picks.values()) for field in fields}
                        def salience(index):
                            item = picks[index]
                            fraction = index / (total - 1)
                            return max(abs((item.get(field) or 0) - ((first.get(field) or 0) + fraction *
                                       ((last.get(field) or 0) - (first.get(field) or 0)))) / (spans[field] or 1)
                                       for field in fields)
                        interior = sorted((index for index in picks if index not in (0, total - 1)),
                                          key=lambda index: (-salience(index), index))
                        keep = {0, total - 1, *interior[:max_points - 2]}
                        picks = {index: item for index, item in picks.items() if index in keep}
                    items = [picks[index] for index in sorted(picks)]
            result = {"items": items, "total_rows": source_total, "sampled": source_total > len(items)}
            db.execute("INSERT OR REPLACE INTO chart_cache VALUES (?,?,(SELECT coalesce(max(touched),0)+1 FROM chart_cache))",
                       (key, json.dumps(result, ensure_ascii=False)))
            db.execute("DELETE FROM chart_cache WHERE cache_key IN (SELECT cache_key FROM chart_cache ORDER BY touched DESC LIMIT -1 OFFSET ?)",
                       (CHART_CACHE_LIMIT,))
        return result


class PartialResultIndex:
    """Query worker progress in place without materializing its seven tables."""

    def __init__(self, control: Path):
        self.path = control / "partial.sqlite3"

    def _ensure_indices(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=60)) as db, db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='partial_order_id'").fetchone():
                return
            db.execute("BEGIN IMMEDIATE")
            for name, expressions in (
                ("partial_date", f"table_name,{PARTIAL_COLUMNS['date']}"),
                ("partial_symbol_date", f"table_name,{PARTIAL_COLUMNS['symbol']},{PARTIAL_COLUMNS['date']}"),
                ("partial_signal_date", f"table_name,{PARTIAL_COLUMNS['signal_date']}"),
                ("partial_execution_date", f"table_name,{PARTIAL_COLUMNS['execution_date']}"),
                ("partial_order_id", f"table_name,{PARTIAL_COLUMNS['order_id']}"),
            ):
                db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON rows({expressions})")

    def page(self, name: str, offset: int, limit: int, date_from: str | None, date_to: str | None,
             symbol: str | None, context_date: str | None) -> tuple[list[dict], int]:
        if not self.path.is_file():
            return [], 0
        self._ensure_indices()
        where, params = ResultIndex._where(name, date_from, date_to, symbol, context_date, PARTIAL_COLUMNS)
        order = f"{PARTIAL_COLUMNS['date']},rowid" if date_from or date_to or symbol or context_date else "rowid"
        with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=60)) as db:
            db.execute("BEGIN")
            total = db.execute(f"SELECT count(*) FROM rows WHERE {where}", params).fetchone()[0]
            items = [json.loads(row[0]) for row in db.execute(
                f"SELECT payload FROM rows WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset))]
        return items, total
