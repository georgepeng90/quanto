"""The market library location belongs to the user, independently of workspaces."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def _config_path() -> Path:
    path = Path(os.environ.get("QUANTO_MARKET_CONFIG", Path.home() / ".quanto" / "market.json"))
    if not path.is_absolute():
        raise ValueError("market configuration path must be absolute")
    return path


def _absolute_root(root: Path) -> Path:
    root = Path(root).expanduser()
    if not root.is_absolute():
        raise ValueError("market root must be absolute")
    return root.resolve()


def get_market_root() -> Path:
    path = _config_path()
    if not path.exists():
        return Path.home() / ".quanto" / "market"
    return _absolute_root(json.loads(path.read_text(encoding="utf-8"))["root"])


def configure_market_root(root: Path) -> Path:
    root = _absolute_root(root)
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump({"root": str(root)}, stream, ensure_ascii=False)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return root
