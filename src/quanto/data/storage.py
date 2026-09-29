from __future__ import annotations

import json
from pathlib import Path

from quanto.data.schemas import DatasetBundle


def write_parquet_dataset(bundle: DatasetBundle, destination: Path) -> None:
    """Publish a validated bundle into a new, otherwise empty directory."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("dataset destination must be empty")

    tables = {name: getattr(bundle, name) for name in
        ("prices", "benchmark", "calendar", "instruments", "status", "events")}
    for name, table in tables.items():
        if name == "prices":
            table = table.sort_values(["symbol", "date"])
        table.to_parquet(destination / f"{name}.parquet", index=False, row_group_size=8192)

    auxiliary_paths = {}
    for index, name in enumerate(sorted(bundle.manifest.auxiliary_tables)):
        filename = f"auxiliary_{index:04d}.parquet"
        bundle.auxiliary[name].to_parquet(destination / filename, index=False)
        auxiliary_paths[name] = filename
    manifest = bundle.manifest.model_copy(update={"auxiliary_tables": auxiliary_paths})
    (destination / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True), encoding="utf-8")
