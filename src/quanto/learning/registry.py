"""Publish learning artifacts only with a successful supervised job transaction."""
from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import re

from quanto.workbench.revisions import json_bytes


LEARNING_KINDS = {"prepare_learning": "dataset", "train": "model", "model_backtest": "backtest",
                  "train_policy": "policy", "policy_backtest": "policy_backtest"}


def migrate_learning_schema(db) -> None:
    ddl = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()[0]
    missing = [kind for kind in LEARNING_KINDS if f"'{kind}'" not in ddl]
    if missing:
        replacement = re.sub(r'CREATE TABLE (?:"jobs"|jobs)(?=\s|\()',
                             'CREATE TABLE jobs_learning_migration', ddl, count=1, flags=re.IGNORECASE)
        replacement = replacement.replace("'validate_strategy'", "'validate_strategy'," + ','.join(f"'{kind}'" for kind in missing), 1)
        db.execute(replacement)
        columns = ','.join(row[1] for row in db.execute('PRAGMA table_info(jobs)'))
        db.execute(f'INSERT INTO jobs_learning_migration ({columns}) SELECT {columns} FROM jobs')
        db.execute('DROP TABLE jobs')
        db.execute('ALTER TABLE jobs_learning_migration RENAME TO jobs')
    db.execute('''CREATE TABLE IF NOT EXISTS learning_artifacts (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
        content_hash TEXT NOT NULL, manifest_json TEXT NOT NULL,
        files_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS learning_submissions (
        job_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS investing_models (
        id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
        spec_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS investing_sessions (
        job_id TEXT PRIMARY KEY, model_id TEXT NOT NULL, base_version_id TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS investing_sessions_model ON investing_sessions(model_id)')


def runtime_versions() -> dict:
    versions = {'python': platform.python_version()}
    for name in ('numpy', 'pandas', 'pyarrow', 'torch', 'stable-baselines3', 'gymnasium', 'cloudpickle'):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    return versions


def verify_runtime(expected: dict) -> None:
    if runtime_versions() != expected:
        raise ValueError('ENVIRONMENT_CHANGED: dependencies changed after submission; submit a new job')


def verify_submission(store, identity: str, path: Path) -> None:
    with store.transaction() as db:
        row = db.execute('SELECT input_hash FROM learning_submissions WHERE job_id=?', (identity,)).fetchone()
    if row is None or hashlib.sha256(path.read_bytes()).hexdigest() != row[0]:
        raise ValueError('INPUT_CHANGED: frozen learning request changed after submission')


def hash_files(root: Path) -> dict[str, str]:
    files = {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError('INVALID_ARTIFACT: artifact directory missing or symbolic')
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('INVALID_ARTIFACT: symbolic artifact file')
        if path.is_file():
            with path.open('rb') as stream:
                files[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return files


def verify_ref(ref: dict) -> Path:
    root = Path(ref['path'])
    if hash_files(root) != ref['files']:
        raise ValueError('INVALID_ARTIFACT: dataset or model changed')
    return root


def finish_learning_job(store, identity: str, final: Path, result: dict) -> bool:
    from quanto.workbench.store import utc_now
    job = store.get_job(identity)
    if job.state != 'running':
        return False
    kind = LEARNING_KINDS[job.kind]
    if result.get('artifact_kind') != kind:
        raise ValueError('INVALID_ARTIFACT: unexpected kind')
    root = final / 'artifact'
    files = hash_files(root)
    expected = {name.removeprefix('artifact/'): value for name, value in result['hashes'].items() if name.startswith('artifact/')}
    if files != expected:
        raise ValueError('INVALID_ARTIFACT: publication hash mismatch')
    name = 'summary.json' if kind in {'backtest', 'policy_backtest'} else 'manifest.json'
    manifest = json.loads((root / name).read_text(encoding='utf-8'))
    with store.transaction() as db:
        state = db.execute('SELECT state FROM jobs WHERE id=?', (identity,)).fetchone()
        if state is None or state[0] != 'running':
            return False
        db.execute('INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)',
                   (identity, kind, str(root), hashlib.sha256(json_bytes(files)).hexdigest(),
                    json_bytes(manifest).decode(), json_bytes(files).decode(), utc_now()))
        db.execute("UPDATE jobs SET state='succeeded',finished_at=? WHERE id=?", (utc_now(), identity))
    return True
