"""
Risk layer — applied to every signal before it becomes a trade.

Three responsibilities, exactly as specified:
  1. Hard 1% stop: dollar loss if stop is hit == risk_pct * equity.
  2. Volatility-adjusted sizing: size derived from the ATR-based stop
     distance, so every trade risks the same dollars regardless of
     instrument volatility.
  3. Correlation filter: block (or shrink) a new entry that is too
     correlated with something already open, to prevent double exposure.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .types import Signal


class RiskManager:
    def __init__(
        self,
        risk_pct: float = 0.01,         # 1% of equity risked per trade
        corr_threshold: float = 0.7,    # block new entry above this |corr|
        corr_lookback: int = 100,       # bars of returns for correlation
        corr_action: str = "block",     # 'block' or 'shrink'
        max_concurrent: int | None = None,
    ):
        self.risk_pct = risk_pct
        self.corr_threshold = corr_threshold
        self.corr_lookback = corr_lookback
        self.corr_action = corr_action
        self.max_concurrent = max_concurrent

    def size_for(self, signal: Signal, equity: float) -> float:
        """Units to trade so loss-at-stop == risk_pct * equity.

        risk_dollars = risk_pct * equity
        stop_distance = |entry - stop|
        size = risk_dollars / stop_distance
        """
        stop_distance = abs(signal.entry - signal.stop)
        if stop_distance <= 0 or not np.isfinite(stop_distance):
            return 0.0
        risk_dollars = self.risk_pct * equity
        return risk_dollars / stop_distance

    def correlation_ok(
        self,
        signal: Signal,
        open_symbols: list[str],
        returns_panel: pd.DataFrame,
    ) -> tuple[bool, float]:
        """Return (allowed, max_abs_corr_vs_open).

        returns_panel: recent aligned returns, columns=symbols. We compare
        the candidate symbol against each currently-open symbol.
        """
        if not open_symbols or signal.symbol not in returns_panel.columns:
            return True, 0.0
        peers = [s for s in open_symbols if s in returns_panel.columns and s != signal.symbol]
        if not peers:
            return True, 0.0
        window = returns_panel.tail(self.corr_lookback)
        cand = window[signal.symbol]
        max_abs = 0.0
        for p in peers:
            c = cand.corr(window[p])
            if np.isfinite(c):
                max_abs = max(max_abs, abs(c))
        return max_abs < self.corr_threshold, max_abs
