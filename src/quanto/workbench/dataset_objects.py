from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
import tempfile

from quanto.objects import digest, json_bytes, safe_name, file_hash as _file_hash, file_paths as _paths


_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _hashes(root: Path) -> dict[str, str]:
    return {name: _file_hash(path) for name, path in _paths(root).items()}


def verify_dataset_version(version) -> Path:
    if digest(json_bytes(_hashes(version.path))) != version.content_hash:
        raise ValueError("registered dataset hash mismatch")
    return version.path


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
