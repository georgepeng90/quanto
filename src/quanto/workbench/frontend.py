"""Checkout delivery: bind the static build to its source and output bytes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def frontend_sources(web: Path) -> dict[str, str]:
    files = [web / name for name in ("package.json", "package-lock.json", "index.html", "vite.config.ts", "tsconfig.json")]
    files.extend((web / "src").rglob("*"))
    files.extend((web / "public").rglob("*"))
    return {path.relative_to(web).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files if path.is_file()}


def validate_frontend(checkout: Path, frontend: Path) -> None:
    instruction = "请在当前仓库执行 npm.cmd --prefix web ci 和 npm.cmd --prefix web run build"
    try:
        manifest = json.loads((frontend / "build-manifest.json").read_text(encoding="utf-8"))
        if manifest["sources"] != frontend_sources(checkout / "web"):
            raise ValueError("source mismatch")
        outputs = {path.relative_to(frontend).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in frontend.rglob("*") if path.is_file() and path.name != "build-manifest.json"}
        if "index.html" not in outputs or manifest["outputs"] != outputs:
            raise ValueError("output mismatch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"FRONTEND_BUILD_INVALID: 前端构建缺失、损坏或与当前源码不匹配。{instruction}") from exc
