import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from quanto.learning.registry import hash_files, verify_submission
from quanto.learning.service import LearningService
from quanto.workbench.service import WorkbenchService
from quanto.workbench.store import WorkbenchStore


def source_workspace(tmp_path):
    store = WorkbenchStore(tmp_path / 'learning')
    root = store.artifact_root / 'policy-job' / 'final' / 'artifact'
    root.mkdir(parents=True)
    (root / 'manifest.json').write_text('{"kind":"policy","name":"preserved"}')
    (root / 'policy.zip').write_bytes(b'policy weights')
    inputs = store.root / 'job-inputs' / 'policy-job'
    inputs.mkdir(parents=True)
    (inputs / 'input.json').write_text(json.dumps({'source_root': str(inputs), 'input': {'request': {}}}))
    files = hash_files(root)
    with store.transaction() as db:
        db.execute("INSERT INTO jobs (id,kind,state,enqueue_seq,artifact_root,input_path) VALUES ('policy-job','train_policy','succeeded',1,?,?)",
                   (str(store.artifact_root), str(inputs / 'input.json')))
        db.execute('INSERT INTO learning_submissions VALUES (?,?)', ('policy-job', hashlib.sha256((inputs / 'input.json').read_bytes()).hexdigest()))
        db.execute('INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)',
                   ('policy-job', 'policy', str(root), 'content-id', '{"kind":"policy"}', json.dumps(files), '2026-10-03'))
    return store


def destination_workspace(tmp_path):
    store = WorkbenchStore(tmp_path / 'research')
    with store.transaction() as db:
        db.execute("INSERT INTO jobs (id,kind,state,run_id,enqueue_seq,artifact_root) VALUES ('old-job','backtest','succeeded','old-run',50,?)", (str(store.artifact_root),))
        db.execute("INSERT INTO runs VALUES ('old-run','old-job','experiment',1,'unchanged-manifest','hash','agent',NULL,NULL)")
    return store


def consolidate(*args, **kwargs):
    from quanto.workbench.consolidation import consolidate_learning_workspace
    return consolidate_learning_workspace(*args, **kwargs)


def test_preview_does_not_change_either_workspace(tmp_path):
    source, dest = source_workspace(tmp_path), destination_workspace(tmp_path)
    before = hash_files(tmp_path)
    report = consolidate(source.root, dest.root)
    assert report['counts']['jobs'] == 1
    assert report['applied'] is False
    assert hash_files(tmp_path) == before


def test_import_preserves_frozen_bytes_old_runs_and_works_without_source(tmp_path):
    source, dest = source_workspace(tmp_path), destination_workspace(tmp_path)
    before = hash_files(source.root)
    report = consolidate(source.root, dest.root, apply=True)
    assert report['applied'] is True
    assert hash_files(source.root) == before
    assert Path(report['backup']).joinpath('source.sqlite3').is_file()
    with sqlite3.connect(Path(report['backup']) / 'destination.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    job = dest.get_job('policy-job')
    assert job.enqueue_seq == 51
    assert job.artifact_root.is_relative_to(dest.root)
    assert Path(job.input_path).is_relative_to(dest.root)
    verify_submission(dest, job.id, Path(job.input_path))
    with dest.transaction() as db:
        assert db.execute('SELECT manifest_path FROM runs').fetchone()[0] == 'unchanged-manifest'
    # Imported policy references are self-contained, even with the source offline.
    source.root.rename(tmp_path / 'offline-source')
    service = LearningService(WorkbenchService(dest.root))
    ref = service.reference('policy-job', 'policy')
    assert Path(ref['path']).is_relative_to(dest.root)
    assert (Path(ref['path']) / 'policy.zip').read_bytes() == b'policy weights'


@pytest.mark.parametrize('which', ['source', 'destination'])
def test_rejects_active_jobs_without_mutation(tmp_path, which):
    source, dest = source_workspace(tmp_path), destination_workspace(tmp_path)
    target = source if which == 'source' else dest
    with target.transaction() as db:
        db.execute("UPDATE jobs SET state='running'")
    before = hash_files(tmp_path)
    with pytest.raises(ValueError, match='active jobs'):
        consolidate(source.root, dest.root, apply=True)
    assert hash_files(tmp_path) == before


def test_duplicate_import_rejected_before_writing(tmp_path):
    source, dest = source_workspace(tmp_path), destination_workspace(tmp_path)
    consolidate(source.root, dest.root, apply=True)
    before = hash_files(tmp_path)
    with pytest.raises(ValueError, match='collision'):
        consolidate(source.root, dest.root, apply=True)
    assert hash_files(tmp_path) == before


def test_source_with_fixed_runs_is_rejected(tmp_path):
    source, dest = destination_workspace(tmp_path), WorkbenchStore(tmp_path / 'other')
    with pytest.raises(ValueError, match='learning-only'):
        consolidate(source.root, dest.root, apply=True)


def test_corrupt_source_artifact_rejected_before_backup_or_import(tmp_path):
    source, dest = source_workspace(tmp_path), destination_workspace(tmp_path)
    (source.artifact_root / 'policy-job/final/artifact/policy.zip').write_bytes(b'changed weights')
    before = hash_files(tmp_path)
    with pytest.raises(ValueError, match='changed'):
        consolidate(source.root, dest.root, apply=True)
    assert hash_files(tmp_path) == before
