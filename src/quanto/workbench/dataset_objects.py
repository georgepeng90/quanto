from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

from quanto.workbench.revisions import digest, json_bytes, safe_name


_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _paths(root: Path) -> dict[str, Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("dataset object path invalid")
    result = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            if (Path(current) / name).is_symlink():
                raise ValueError("dataset object contains symbolic link")
        for name in files:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            safe_name(relative)
            result[relative] = path
    return result


def _hashes(root: Path) -> dict[str, str]:
    return {name: _file_hash(path) for name, path in _paths(root).items()}


def verify_dataset_object(workspace: Path, ref: dict) -> Path:
    if not isinstance(ref, dict) or set(ref) != {"version_id", "content_hash", "files"}:
        raise ValueError("dataset reference invalid")
    identity, files = ref["content_hash"], ref["files"]
    if not isinstance(identity, str) or not _HASH.fullmatch(identity) or not isinstance(ref["version_id"], str) or not isinstance(files, dict) or not files:
        raise ValueError("dataset reference invalid")
    for name, expected in files.items():
        safe_name(name)
        if not isinstance(expected, str) or not _HASH.fullmatch(expected):
            raise ValueError("dataset reference invalid")
    if digest(json_bytes(files)) != identity:
        raise ValueError("dataset reference hash mismatch")
    root = workspace / "dataset_objects"
    if root.is_symlink():
        raise ValueError("dataset object path invalid")
    target = root / identity
    actual = _hashes(target)
    if set(actual) != set(files):
        raise ValueError("dataset object file set mismatch")
    if actual != files:
        raise ValueError("dataset object hash mismatch")
    return target


def ensure_dataset_object(workspace: Path, version) -> tuple[Path, dict[str, str]]:
    identity = version.content_hash
    if not isinstance(identity, str) or not _HASH.fullmatch(identity):
        raise ValueError("registered dataset hash invalid")
    parent = workspace / "dataset_objects"
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError("dataset object path invalid")
    source = _paths(version.path)
    target = parent / identity
    if target.exists() or target.is_symlink():
        files = _hashes(version.path)
        if digest(json_bytes(files)) != identity:
            raise ValueError("registered dataset hash mismatch")
        verify_dataset_object(workspace, {"version_id": version.id, "content_hash": identity, "files": files})
        return target, files
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
    try:
        hashes = {}
        for name, path in sorted(source.items()):
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            value = hashlib.sha256()
            with path.open("rb") as input_stream, destination.open("wb") as output_stream:
                for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                    output_stream.write(chunk)
                    value.update(chunk)
            hashes[name] = value.hexdigest()
        if digest(json_bytes(hashes)) != identity:
            raise ValueError("registered dataset hash mismatch")
        if _hashes(stage) != hashes:
            raise OSError("staged dataset verification failed")
        try:
            stage.rename(target)
        except FileExistsError:
            pass
        verify_dataset_object(workspace, {"version_id": version.id, "content_hash": identity, "files": hashes})
        return target, hashes
    finally:
        if stage.exists():
            shutil.rmtree(stage)
