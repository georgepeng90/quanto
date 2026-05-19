from __future__ import annotations

from dataclasses import dataclass
import ast
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - depends on local environment
    yaml = None


@dataclass(frozen=True)
class Paths:
    root: Path
    strategy_config: Path
    universe_config: Path
    prices: Path
    benchmark: Path
    outputs: Path


@dataclass(frozen=True)
class AppConfig:
    raw: dict[str, Any]
    path: Path

    @property
    def initial_cash(self) -> float:
        return float(self.raw["backtest"].get("initial_cash", 100000))

    @property
    def commission_rate(self) -> float:
        return float(self.raw["backtest"].get("commission_rate", 0.0005))

    @property
    def slippage_rate(self) -> float:
        return float(self.raw["backtest"].get("slippage_rate", 0.0005))

    @property
    def top_n(self) -> int:
        return int(self.raw["strategy"].get("top_n", 3))

    @property
    def output_dir(self) -> Path:
        return Path(self.raw["backtest"].get("output_dir", "outputs/backtests"))

    @property
    def data_dir(self) -> Path:
        return Path(self.raw["backtest"].get("data_dir", "data/processed"))


def load_yaml(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        data = yaml.safe_load(text) or {}
    else:
        data = _load_minimal_yaml(text)
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"", "null", "Null", "NULL"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def _load_minimal_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    lines = [line.rstrip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    for index, raw in enumerate(lines):
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if line.startswith("- "):
            item_text = line[2:]
            if not isinstance(parent, list):
                raise ValueError("List item without list parent in fallback YAML parser")
            if ":" in item_text:
                key, value = item_text.split(":", 1)
                item = {key.strip(): _parse_scalar(value)}
                parent.append(item)
                stack.append((indent, item))
            else:
                parent.append(_parse_scalar(item_text))
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            parent[key] = _parse_scalar(value)
            continue
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        child: Any = [] if next_line.strip().startswith("- ") else {}
        parent[key] = child
        stack.append((indent, child))
    return root


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    return AppConfig(raw=load_yaml(path), path=path)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_paths(root: str | Path | None = None) -> Paths:
    base = Path(root) if root else project_root()
    return Paths(
        root=base,
        strategy_config=base / "configs" / "strategy.yaml",
        universe_config=base / "configs" / "universe.yaml",
        prices=base / "data" / "processed" / "prices.csv",
        benchmark=base / "data" / "processed" / "benchmark.csv",
        outputs=base / "outputs" / "backtests",
    )


def load_strategy_config(path: str | Path) -> dict[str, Any]:
    cfg = load_yaml(path)
    for section in ("strategy", "factors", "risk", "backtest"):
        if section not in cfg:
            raise ValueError(f"Missing required config section: {section}")
    return cfg
