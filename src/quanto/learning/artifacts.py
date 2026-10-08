from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from quanto.factors.features import FrozenTransform
from quanto.framework import DefinitionRef, thaw_json
from quanto.objects import digest, file_hash, file_paths, json_bytes, verified_files


_TRANSFORM_FILE = "transform.json"
_TRANSFORM_FIELDS = frozenset({
    "definition_ref", "definition_hash", "params_hash", "recipe", "training_start",
    "training_end", "training_cutoff", "training_frame_hashes", "training_dataset_hashes",
    "training_binding_hashes", "data", "transform_hash",
})


def write_frozen_transform(root: Path, transform: FrozenTransform) -> dict:
    payload = {
        "definition_ref": transform.definition_ref.model_dump(mode="json"),
        "definition_hash": transform.definition_hash, "params_hash": transform.params_hash,
        "recipe": transform.recipe, "training_start": transform.training_start.isoformat(),
        "training_end": transform.training_end.isoformat(), "training_cutoff": transform.training_cutoff.isoformat(),
        "training_frame_hashes": transform.training_frame_hashes,
        "training_dataset_hashes": transform.training_dataset_hashes,
        "training_binding_hashes": transform.training_binding_hashes,
        "data": thaw_json(transform.data), "transform_hash": transform.transform_hash,
    }
    (Path(root) / _TRANSFORM_FILE).write_bytes(json_bytes(payload))
    return {"path": _TRANSFORM_FILE, "transform_hash": transform.transform_hash}


def read_frozen_transform(root: Path, descriptor: dict) -> FrozenTransform:
    try:
        if (not isinstance(descriptor, dict) or set(descriptor) != {"path", "transform_hash"}
                or descriptor["path"] != _TRANSFORM_FILE):
            raise ValueError("invalid transform descriptor")
        payload = json.loads((Path(root) / _TRANSFORM_FILE).read_bytes())
        if not isinstance(payload, dict) or set(payload) != _TRANSFORM_FIELDS:
            raise ValueError("invalid transform fields")
        for key in ("training_frame_hashes", "training_dataset_hashes", "training_binding_hashes"):
            if not isinstance(payload[key], list) or not all(isinstance(value, str) for value in payload[key]):
                raise ValueError("invalid transform coverage")
        if not isinstance(payload["data"], dict):
            raise ValueError("invalid transform data")
        transform = FrozenTransform(
            DefinitionRef.model_validate(payload["definition_ref"]), payload["definition_hash"],
            payload["params_hash"], payload["recipe"], date.fromisoformat(payload["training_start"]),
            date.fromisoformat(payload["training_end"]), datetime.fromisoformat(payload["training_cutoff"]),
            tuple(payload["training_frame_hashes"]), tuple(payload["training_dataset_hashes"]),
            tuple(payload["training_binding_hashes"]), payload["data"],
        )
        if payload["transform_hash"] != transform.transform_hash or descriptor["transform_hash"] != transform.transform_hash:
            raise ValueError("transform identity hash mismatch")
        return transform
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"transform integrity error: {exc}") from exc


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
