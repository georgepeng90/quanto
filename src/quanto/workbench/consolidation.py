"""Offline import of a learning-only workspace into an existing research workspace.

Stop both servers before applying. Historical frozen JSON is copied verbatim:
its original absolute paths are provenance, while live registry pointers move.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from quanto.learning.registry import LEARNING_KINDS, hash_files, verify_ref
from quanto.workbench.store import WorkbenchStore


TABLES = ('datasets', 'dataset_market_sources', 'strategies', 'jobs',
          'learning_artifacts', 'learning_submissions')
DIRECTORIES = ('datasets', 'dataset_objects', 'strategies', 'job-inputs', 'artifacts')
TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}


def _read(root):
    with closing(sqlite3.connect((root / 'workbench.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return {row[0]: [dict(item) for item in db.execute(f'SELECT * FROM "{row[0]}"')]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _backup(root, path):
    with closing(sqlite3.connect((root / 'workbench.sqlite3').as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(path)) as target:
            source.backup(target)


def consolidate_learning_workspace(source: Path, destination: Path, *, apply: bool = False) -> dict:
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError('source and destination must be separate workspaces')
    original, existing = _read(source), _read(destination)
    allowed = set(TABLES) | {'schema_version', 'runs', 'experiments', 'idempotency', 'result_views'}
    if any(original.get(name) for name in ('runs', 'experiments', 'idempotency', 'result_views')) or any(
            rows for name, rows in original.items() if name not in allowed):
        raise ValueError('source must be a learning-only workspace')
    for workspace in (original, existing):
        if any(job['state'] not in TERMINAL for job in workspace['jobs']):
            raise ValueError('cannot consolidate workspaces with active jobs')
    if any(job['kind'] not in LEARNING_KINDS for job in original['jobs']):
        raise ValueError('source must contain learning-only jobs')
    for table in TABLES:
        key = 'dataset_version_id' if table == 'dataset_market_sources' else 'job_id' if table == 'learning_submissions' else 'id'
        overlap = {row[key] for row in original.get(table, [])} & {row[key] for row in existing.get(table, [])}
        if overlap:
            raise ValueError(f'identifier collision in {table}: {sorted(overlap)}')
    identity = hashlib.sha256(str(source).encode()).hexdigest()[:12]
    imported = destination / 'imports' / f'{source.name}-{identity}'
    if imported.exists():
        raise ValueError('import directory collision; inspect the prior import before retrying')

    def relocated(value):
        path = Path(value).resolve()
        if not path.is_relative_to(source):
            raise ValueError('source references files outside its workspace')
        relative = path.relative_to(source)
        if not relative.parts or relative.parts[0] not in DIRECTORIES or not path.exists():
            raise ValueError(f'missing or unsupported source path: {path}')
        return str(imported / relative)

    rows = {table: [dict(row) for row in original.get(table, [])] for table in TABLES}
    for table in ('datasets', 'strategies', 'learning_artifacts'):
        for row in rows[table]:
            row['path'] = relocated(row['path'])
    sequence = max((job['enqueue_seq'] for job in existing['jobs']), default=0)
    rows['jobs'].sort(key=lambda job: job['enqueue_seq'])
    for index, job in enumerate(rows['jobs'], 1):
        job['enqueue_seq'] = sequence + index
        job['artifact_root'] = relocated(job['artifact_root'])
        if job['input_path']:
            job['input_path'] = relocated(job['input_path'])
    for row in original.get('learning_artifacts', []):
        verify_ref({'path': row['path'], 'files': json.loads(row['files_json'])})
    jobs = {job['id']: job for job in original['jobs']}
    for row in original.get('learning_submissions', []):
        raw = Path(jobs[row['job_id']]['input_path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['input_hash']:
            raise ValueError('frozen submission hash mismatch')
    hashes = {name: hash_files(source / name) for name in DIRECTORIES if (source / name).exists()}
    report = {'applied': False, 'source': str(source), 'destination': str(destination),
              'imported_root': str(imported), 'counts': {table: len(values) for table, values in rows.items()},
              'preserved_runs': len(existing.get('runs', [])),
              'note': 'Frozen input and manifest bytes retain historical paths; registry paths are relocated.'}
    if not apply:
        return report
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    backup = destination / 'backups' / f'consolidation-{stamp}'
    backup.mkdir(parents=True)
    _backup(source, backup / 'source.sqlite3')
    _backup(destination, backup / 'destination.sqlite3')
    for name in hashes:
        shutil.copytree(source / name, imported / name)
        if hash_files(imported / name) != hashes[name] or hash_files(source / name) != hashes[name]:
            raise ValueError('source changed or copied files failed hash verification')
    # Refuse to import a stale snapshot if either database changed during copying.
    if _read(source) != original or _read(destination) != existing:
        raise ValueError('workspace changed during import; stop both services before retrying')
    store = WorkbenchStore(destination)
    with store.transaction() as db:
        for table, values in rows.items():
            for row in values:
                columns = ','.join(row)
                db.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
    report.update(applied=True, backup=str(backup), file_hashes=hashes)
    (backup / 'receipt.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
