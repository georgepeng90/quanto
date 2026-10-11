"""Transactional workspace schema migration; file artifacts are never rewritten."""
from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 3
BASE_TABLES = (
    'CREATE TABLE IF NOT EXISTS datasets (id TEXT PRIMARY KEY, path TEXT NOT NULL, content_hash TEXT NOT NULL, manifest_json TEXT NOT NULL)',

    'CREATE TABLE IF NOT EXISTS dataset_market_sources (dataset_version_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, manifest_digest TEXT NOT NULL)',

    'CREATE TABLE IF NOT EXISTS strategies (id TEXT PRIMARY KEY, path TEXT NOT NULL, content_hash TEXT NOT NULL, manifest_json TEXT NOT NULL)',

    'CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload_json TEXT NOT NULL)',

    'CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, experiment_id TEXT NOT NULL, experiment_revision INTEGER NOT NULL, manifest_path TEXT NOT NULL, manifest_hash TEXT NOT NULL, source TEXT NOT NULL, parent_run_id TEXT, group_id TEXT)',

    "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('backtest','demo','fetch','import','validate_strategy')), state TEXT NOT NULL, run_id TEXT UNIQUE, enqueue_seq INTEGER NOT NULL UNIQUE, artifact_root TEXT NOT NULL, pid INTEGER, process_start TEXT, heartbeat TEXT, completed_through TEXT, error TEXT, input_path TEXT, created_at TEXT, started_at TEXT, finished_at TEXT)",

    'CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, run_ids_json TEXT NOT NULL)',
)
FRAMEWORK_TABLES = (
    """CREATE TABLE baskets (
        basket_id TEXT PRIMARY KEY, name TEXT NOT NULL,
        current_revision INTEGER NOT NULL CHECK (current_revision >= 1), created_at TEXT NOT NULL,
        FOREIGN KEY (basket_id, current_revision) REFERENCES basket_revisions(basket_id, revision)
            DEFERRABLE INITIALLY DEFERRED)""",
    """CREATE TABLE basket_revisions (
        basket_id TEXT NOT NULL REFERENCES baskets(basket_id),
        revision INTEGER NOT NULL CHECK (revision >= 1), revision_json TEXT NOT NULL,
        content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY (basket_id, revision))""",
    """CREATE TABLE dataset_asset_bindings (
        dataset_version_id TEXT PRIMARY KEY REFERENCES datasets(id),
        dataset_content_hash TEXT NOT NULL, binding_json TEXT NOT NULL, binding_hash TEXT NOT NULL,
        UNIQUE (dataset_version_id, binding_hash))""",
    """CREATE TABLE model_framework_bindings (
        model_id TEXT PRIMARY KEY REFERENCES investing_models(id), spec_hash TEXT NOT NULL,
        dataset_version_id TEXT NOT NULL, dataset_content_hash TEXT NOT NULL,
        basket_id TEXT NOT NULL, basket_revision INTEGER NOT NULL,
        asset_binding_hash TEXT NOT NULL, binding_json TEXT NOT NULL, binding_hash TEXT NOT NULL,
        FOREIGN KEY (basket_id, basket_revision) REFERENCES basket_revisions(basket_id, revision),
        FOREIGN KEY (dataset_version_id, asset_binding_hash)
            REFERENCES dataset_asset_bindings(dataset_version_id, binding_hash))""",
    """CREATE TABLE framework_idempotency (
        scope TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, response_json TEXT NOT NULL,
        PRIMARY KEY (scope, key))""",
)
CONTEXT_TABLES = (
    """CREATE TABLE IF NOT EXISTS run_contexts (
        job_id TEXT PRIMARY KEY REFERENCES jobs(id),
        context_json TEXT NOT NULL, context_hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS policy_version_contexts (
        version_id TEXT PRIMARY KEY REFERENCES learning_artifacts(id),
        context_json TEXT NOT NULL, context_hash TEXT NOT NULL,
        artifact_content_hash TEXT NOT NULL)""",
)


def migrate_schema(db: sqlite3.Connection) -> None:
    """Caller owns BEGIN/COMMIT, so every DDL and version change rolls back together."""
    if not db.in_transaction or db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("schema migration requires a transaction with foreign keys enabled")
    existing = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
    if existing:
        versions = [row[0] for row in db.execute("SELECT version FROM schema_version")]
        if len(versions) != 1 or versions[0] not in (1, 2, SCHEMA_VERSION):
            raise ValueError("unsupported workbench schema version")
        version = versions[0]
        if version == SCHEMA_VERSION:
            return
    else:
        version = 1
        db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        db.execute("INSERT INTO schema_version VALUES (1)")
    for statement in BASE_TABLES:
        db.execute(statement)
    columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    for column in ("input_path", "created_at", "started_at", "finished_at"):
        if column not in columns:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
    from quanto.learning.registry import migrate_learning_schema
    migrate_learning_schema(db)
    if version == 1:
        for statement in FRAMEWORK_TABLES:
            db.execute(statement)
    for statement in CONTEXT_TABLES:
        db.execute(statement)
    db.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
