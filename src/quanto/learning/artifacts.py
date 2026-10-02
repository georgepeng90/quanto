from __future__ import annotations

import json
from pathlib import Path

from quanto.objects import digest, file_hash, file_paths, json_bytes, verified_files


def write_manifest(root: Path, manifest: dict) -> dict:
    manifest = {**manifest, "files": {name: file_hash(path) for name, path in file_paths(root).items() if name != "manifest.json"}}
    manifest["id"] = digest(json_bytes(manifest))
    (root / "manifest.json").write_bytes(json_bytes(manifest))
    return manifest


def read_manifest(path: Path, kind: str | None = None) -> dict:
    path = Path(path)
    root = path.parent if path.is_file() else path
    try:
        raw = (root / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        identity = manifest["id"]
        if digest(json_bytes({key: value for key, value in manifest.items() if key != "id"})) != identity:
            raise ValueError("manifest identity hash mismatch")
        if kind and manifest["kind"] != kind:
            raise ValueError("artifact kind mismatch")
        verified_files(root, {**manifest["files"], "manifest.json": digest(raw)})
        return manifest
    except (KeyError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"artifact integrity error: {exc}") from exc


def read_model_manifest(path: Path) -> dict:
    return read_manifest(path, "model")
