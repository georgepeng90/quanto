from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from quanto.objects import digest, json_bytes, safe_name


def source_files(root: Path) -> dict[str, bytes]:
    result = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            if (Path(current) / name).is_symlink():
                raise ValueError("symbolic links are not snapshot inputs")
        for name in files:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            safe_name(relative)
            result[relative] = path.read_bytes()
    return result


def publish_files(parent: Path, identity: str, files: dict[str, bytes]) -> tuple[Path, dict[str, str]]:
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / identity
    if destination.exists():
        raise FileExistsError(destination)
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
    try:
        hashes = {}
        for name, content in sorted(files.items()):
            safe_name(name)
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            hashes[name] = digest(content)
        for name, expected in hashes.items():
            if digest((stage / name).read_bytes()) != expected:
                raise OSError("staged file verification failed")
        stage.rename(destination)
        return destination, hashes
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def verify_files(root: Path, hashes: dict[str, str]) -> None:
    for name, expected in hashes.items():
        safe_name(name)
        path = root / name
        if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError(f"snapshot hash mismatch: {name}")
