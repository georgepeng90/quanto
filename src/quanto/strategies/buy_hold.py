from __future__ import annotations

from dataclasses import dataclass, field

from .base import Hold, StrategyContext, TargetPortfolio


@dataclass
class BuyHold:
    warmup_bars: int = field(default=0, init=False)
    _emitted: bool = field(default=False, init=False)

    def should_evaluate(self, ctx: StrategyContext) -> bool:
        return not self._emitted

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio:
        if self._emitted:
            return Hold("initial target already emitted")
        self._emitted = True
        symbols = sorted(set(ctx.eligible_symbols))
        weight = 1 / len(symbols) if symbols else 0
        return TargetPortfolio({symbol: weight for symbol in symbols}, {symbol: "initial equal weight" for symbol in symbols}, {})
