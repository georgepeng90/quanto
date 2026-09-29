from __future__ import annotations

from dataclasses import dataclass
import ast
import copy
import math
from pathlib import Path
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from datetime import date
from jsonschema import Draft202012Validator, ValidationError

if TYPE_CHECKING:
    from quanto.data.schemas import DatasetBundle

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


class StrictSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ExecutionSpec(StrictSpec):
    commission_rate: float = Field(ge=0)
    minimum_commission: float = Field(ge=0)
    slippage_rate: float = Field(ge=0)
    max_stale_sessions: int = Field(ge=0)
    model: Literal["next_open"]


class StressScenario(StrictSpec):
    name: str = Field(min_length=1)
    symbol_shocks: dict[str, float]
    execution_cost_rate: float = Field(ge=0)


class RiskSpec(StrictSpec):
    max_total_weight: float = Field(ge=0, le=1)
    max_symbol_weight: float = Field(ge=0, le=1)
    group_caps: dict[str, float]
    stress_scenarios: list[StressScenario]
    max_stress_loss: float | None = Field(ge=0, default=None)
    drawdown_pause: float | None = Field(ge=0, le=1, default=None)
    pause_action: Literal["block_new", "reduce", "liquidate"]
    pause_target_weight: float | None = Field(ge=0, le=1, default=None)
    recovery_mode: Literal["never", "recorded_approval"]

    @model_validator(mode="after")
    def check_limits(self) -> RiskSpec:
        if any(cap < 0 or cap > 1 for cap in self.group_caps.values()):
            raise ValueError("group caps must be between 0 and 1")
        if self.pause_action == "reduce" and self.pause_target_weight is None:
            raise ValueError("reduce requires pause_target_weight")
        if self.pause_action != "reduce" and self.pause_target_weight is not None:
            raise ValueError("pause_target_weight applies only to reduce")
        if self.max_stress_loss is not None and not self.stress_scenarios:
            raise ValueError("max_stress_loss requires stress_scenarios")
        return self


class MetricSpec(StrictSpec):
    annual_sessions: int = Field(gt=0)
    risk_free_rate: float
    cash_rate: float
    definition_version: str = Field(min_length=1)


class BacktestSpec(StrictSpec):
    start: date
    end: date
    symbols: list[str] = Field(min_length=1)
    initial_cash: float = Field(gt=0)
    seed: int
    execution: ExecutionSpec
    risk: RiskSpec
    metrics: MetricSpec

    @model_validator(mode="after")
    def check_dates(self) -> BacktestSpec:
        if self.start > self.end:
            raise ValueError("start must not follow end")
        if len(set(self.symbols)) != len(self.symbols) or any(not symbol for symbol in self.symbols):
            raise ValueError("symbols must be unique and nonempty")
        return self

    def validate_for_dataset(self, bundle: DatasetBundle) -> BacktestSpec:
        registered = set(bundle.instruments["symbol"])
        if not set(self.symbols).issubset(registered):
            raise ValueError("Backtest symbols must be registered in the dataset")
        if self.start < bundle.manifest.data_start or self.end > bundle.manifest.data_end:
            raise ValueError("Backtest dates must be covered by the dataset")
        return self


def resolve_strategy_params(schema: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    checked_schema = copy.deepcopy(schema)
    if checked_schema.get("type") != "object":
        raise ValueError("Strategy parameter schema must have an object root")

    unsupported = {"$ref", "$dynamicRef", "$defs", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentSchemas", "patternProperties", "unevaluatedProperties", "unevaluatedItems", "contains", "prefixItems"}

    def require_supported(node: Any) -> None:
        if not isinstance(node, dict):
            raise ValueError("Strategy parameter schema nodes must be objects")
        invalid = unsupported & node.keys()
        if invalid:
            raise ValueError(f"Unsupported strategy schema keywords: {sorted(invalid)}")
        node_type = node.get("type")
        if node_type not in ("object", "array", "string", "integer", "number", "boolean", "null"):
            raise ValueError("Strategy parameter schema nodes require a supported type")
        if node.get("additionalProperties") not in (None, False):
            raise ValueError("Strategy parameter objects cannot allow unknown fields")
        if "properties" in node and node_type != "object":
            raise ValueError("Only object schemas can declare properties")
        if "items" in node and node_type != "array":
            raise ValueError("Only array schemas can declare items")
        properties = node.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("Strategy parameter properties must be a mapping")
        for child in properties.values():
            require_supported(child)
        if "items" in node:
            require_supported(node["items"])

    def close_objects(node: dict[str, Any]) -> None:
        if node.get("type") == "object":
            node["additionalProperties"] = False
            for child in node.get("properties", {}).values():
                close_objects(child)
        if node.get("type") == "array" and isinstance(node.get("items"), dict):
            close_objects(node["items"])

    def apply_defaults(node: dict[str, Any], value: Any) -> None:
        if node.get("type") == "object" and isinstance(value, dict):
            for name, child in node.get("properties", {}).items():
                if name not in value and "default" in child:
                    value[name] = copy.deepcopy(child["default"])
                if name in value:
                    apply_defaults(child, value[name])
        elif node.get("type") == "array" and isinstance(value, list) and isinstance(node.get("items"), dict):
            for item in value:
                apply_defaults(node["items"], item)

    def require_json_value(value: Any) -> None:
        if value is None or isinstance(value, (str, bool, int)):
            return
        if isinstance(value, float) and math.isfinite(value):
            return
        if isinstance(value, list):
            for item in value:
                require_json_value(item)
            return
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            for item in value.values():
                require_json_value(item)
            return
        raise ValueError("Strategy parameters must contain finite JSON values")

    require_supported(checked_schema)
    close_objects(checked_schema)
    Draft202012Validator.check_schema(checked_schema)
    effective = copy.deepcopy(params)
    apply_defaults(checked_schema, effective)
    require_json_value(effective)
    try:
        Draft202012Validator(checked_schema).validate(effective)
    except ValidationError as exc:
        raise ValueError(f"Invalid strategy parameters: {exc.message}") from exc
    return effective
