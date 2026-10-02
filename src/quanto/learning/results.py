"""Disk-backed minute result pagination and bounded chart queries."""
from __future__ import annotations

from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3

from quanto.backtest.models import BacktestCancelled


TABLES = {'equity', 'daily_equity', 'orders', 'fills', 'signals', 'positions'}


def build_index(root: Path, cancelled) -> None:
    with closing(sqlite3.connect(root / 'results.sqlite3')) as db:
        db.execute('CREATE TABLE rows (name TEXT, number INTEGER, payload TEXT, PRIMARY KEY(name,number)) WITHOUT ROWID')
        db.execute('CREATE TABLE counts (name TEXT PRIMARY KEY, count INTEGER)')
        for name in sorted(TABLES):
            count, batch = 0, []
            with (root / f'{name}.csv').open(encoding='utf-8', newline='') as stream:
                for count, row in enumerate(csv.DictReader(stream), 1):
                    batch.append((name, count, json.dumps(row, ensure_ascii=False)))
                    if len(batch) >= 2048:
                        if cancelled():
                            raise BacktestCancelled('CANCELLED: 结果索引已取消')
                        db.executemany('INSERT INTO rows VALUES (?,?,?)', batch)
                        db.commit()
                        batch.clear()
            db.executemany('INSERT INTO rows VALUES (?,?,?)', batch)
            db.execute('INSERT INTO counts VALUES (?,?)', (name, count))
            db.commit()


def table(root: Path, name: str, cursor: str | None = None, limit: int = 100) -> dict:
    if name not in TABLES or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid result table or limit')
    offset = int(cursor or '0')
    if offset < 0:
        raise ValueError('invalid table cursor')
    with closing(sqlite3.connect(f'{(root / "results.sqlite3").as_uri()}?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT payload FROM rows WHERE name=? AND number>? ORDER BY number LIMIT ?', (name, offset, limit + 1)).fetchall()
        count = db.execute('SELECT count FROM counts WHERE name=?', (name,)).fetchone()[0]
    return {'items': [json.loads(row[0]) for row in rows[:limit]],
            'next_cursor': str(offset + limit) if len(rows) > limit else None, 'total_count': count}


def chart(root: Path, max_points: int = 1000) -> dict:
    if not 2 <= max_points <= 5000:
        raise ValueError('chart max_points must be between 2 and 5000')
    with closing(sqlite3.connect(f'{(root / "results.sqlite3").as_uri()}?mode=ro', uri=True)) as db:
        count = db.execute("SELECT count FROM counts WHERE name='equity'").fetchone()[0]
        indices = sorted({1 + round(index * (count - 1) / (max_points - 1)) for index in range(min(count, max_points))}) if count > max_points else list(range(1, count + 1))
        points = []
        # Bounded primary-key lookups avoid scanning every minute for each chart view.
        for number in indices:
            row = json.loads(db.execute("SELECT payload FROM rows WHERE name='equity' AND number=?", (number,)).fetchone()[0])
            equity = float(row['equity'])
            drawdown = float(row.get('drawdown', 0))
            points.append({'time': row.get('timestamp', row.get('bar_end', row.get('date'))),
                           'equity': equity, 'drawdown': drawdown})
    return {'points': points, 'total_count': count, 'sampled': count > max_points}
