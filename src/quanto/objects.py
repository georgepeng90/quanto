from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError(f"unsafe path: {name}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in (".", "..") for part in name.split("/")):
        raise ValueError(f"unsafe path: {name}")
    return name


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def file_paths(root: Path) -> dict[str, Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("object path invalid")
    result = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            if (Path(current) / name).is_symlink():
                raise ValueError("object contains symbolic link")
        for name in files:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            safe_name(relative)
            result[relative] = path
    return result


def verified_files(root: Path, expected: dict[str, str]) -> dict[str, Path]:
    files = file_paths(root)
    if set(files) != set(expected) or any(file_hash(path) != expected[name] for name, path in files.items()):
        raise ValueError("object file set or hash mismatch")
    return files
