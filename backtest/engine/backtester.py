"""
Event-driven, multi-instrument backtester.

Key design points:
  * Unified clock: all instruments' bars are merged onto one ascending
    timeline so the correlation filter sees a TRUE concurrent portfolio.
  * No lookahead: signals act at the bar they fire (entry at that bar's
    close); stops/exits are evaluated on subsequent bars' high/low.
  * Portfolio risk: sizing uses live equity; correlation filter checks the
    candidate against currently-open symbols using trailing returns.
  * One position per symbol at a time (keeps accounting clean).

Each instrument is configured with: bars df, a signals list (precomputed),
and an exit function.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .risk import RiskManager
from .types import Signal, Trade


@dataclass
class Instrument:
    symbol: str
    timeframe: str
    df: pd.DataFrame
    signals: list[Signal]
    exit_fn: Callable                      # (trade, bar, df, i) -> str|None
    exit_kwargs: dict = field(default_factory=dict)
    # internal:
    _sig_by_ts: dict = field(default_factory=dict, repr=False)
    _idx_by_ts: dict = field(default_factory=dict, repr=False)


class Backtester:
    def __init__(
        self,
        instruments: list[Instrument],
        risk: RiskManager,
        starting_equity: float = 100_000.0,
        fee_bps: float = 1.0,              # round-trip cost approx, per side
    ):
        self.instruments = {ins.symbol: ins for ins in instruments}
        self.risk = risk
        self.equity = starting_equity
        self.start_equity = starting_equity
        self.fee_bps = fee_bps

        self.open_trades: dict[str, Trade] = {}
        self.closed_trades: list[Trade] = []
        self.equity_curve: list[tuple[pd.Timestamp, float]] = []
        self.blocked_log: list[dict] = []

        for ins in instruments:
            ins._sig_by_ts = {s.ts: s for s in ins.signals}
            ins._idx_by_ts = {ts: i for i, ts in enumerate(ins.df.index)}

        # Build a returns panel (close-to-close) for the correlation filter,
        # reindexed onto the unified clock with forward fill.
        self._build_unified_clock()

    def _build_unified_clock(self):
        all_ts = sorted(set().union(*[set(ins.df.index) for ins in self.instruments.values()]))
        self.clock = pd.DatetimeIndex(all_ts)
        closes = {}
        for sym, ins in self.instruments.items():
            closes[sym] = ins.df["close"].reindex(self.clock).ffill()
        self.close_panel = pd.DataFrame(closes, index=self.clock)

        # Correlation panel: resample every instrument's NATIVE returns to a
        # common coarse frequency (1h) and align. This avoids the zero-variance
        # runs that forward-filling a 4h series onto a 15m clock would create,
        # which previously produced NaN/degenerate correlations.
        common = "1h"
        ret_cols = {}
        for sym, ins in self.instruments.items():
            c = ins.df["close"].resample(common).last().ffill()
            ret_cols[sym] = c.pct_change()
        self.returns_panel = pd.DataFrame(ret_cols).dropna(how="all")

    def _apply_fee(self, notional: float) -> float:
        return abs(notional) * (self.fee_bps / 10_000.0)

    def _close_trade(self, trade: Trade, ts, price: float, reason: str):
        gross = (price - trade.entry) * trade.size * trade.direction
        fees = self._apply_fee(trade.entry * trade.size) + self._apply_fee(price * trade.size)
        trade.exit, trade.exit_ts, trade.exit_reason = price, ts, reason
        trade.pnl = gross - fees
        risk_per_unit = abs(trade.entry - trade.stop)
        init_risk = risk_per_unit * trade.size
        trade.r_multiple = trade.pnl / init_risk if init_risk > 0 else 0.0
        self.equity += trade.pnl
        self.closed_trades.append(trade)
        del self.open_trades[trade.symbol]

    def run(self) -> "BacktestResult":
        for ts in self.clock:
            # 1) Manage open trades first (stops + strategy exits) on this bar.
            for sym in list(self.open_trades.keys()):
                ins = self.instruments[sym]
                if ts not in ins._idx_by_ts:
                    continue
                i = ins._idx_by_ts[ts]
                bar = ins.df.iloc[i]
                trade = self.open_trades[sym]

                # Hard stop: check intrabar high/low against stop level.
                if trade.direction == +1 and bar["low"] <= trade.stop:
                    self._close_trade(trade, ts, trade.stop, "stop")
                    continue
                if trade.direction == -1 and bar["high"] >= trade.stop:
                    self._close_trade(trade, ts, trade.stop, "stop")
                    continue

                reason = ins.exit_fn(trade, bar, ins.df, i, **ins.exit_kwargs)
                if reason:
                    self._close_trade(trade, ts, bar["close"], reason)

            # 2) Consider new entries for instruments that fired a signal here.
            for sym, ins in self.instruments.items():
                if sym in self.open_trades:
                    continue
                sig = ins._sig_by_ts.get(ts)
                if sig is None:
                    continue
                if self.risk.max_concurrent and len(self.open_trades) >= self.risk.max_concurrent:
                    self.blocked_log.append({"ts": ts, "symbol": sym, "reason": "max_concurrent"})
                    continue

                open_syms = list(self.open_trades.keys())
                ok, max_corr = self.risk.correlation_ok(sig, open_syms, self.returns_panel.loc[:ts])
                if not ok:
                    if self.risk.corr_action == "block":
                        self.blocked_log.append(
                            {"ts": ts, "symbol": sym, "reason": "correlation",
                             "max_corr": round(float(max_corr), 3), "vs": open_syms}
                        )
                        continue
                    # 'shrink' handled below via size factor

                size = self.risk.size_for(sig, self.equity)
                if self.risk.corr_action == "shrink" and not ok:
                    size *= max(0.0, 1.0 - max_corr)  # taper by correlation
                if size <= 0:
                    continue

                self.open_trades[sym] = Trade(
                    symbol=sym, strategy=sig.strategy, direction=sig.direction,
                    entry_ts=ts, entry=sig.entry, stop=sig.stop, size=size,
                )

            # 3) Mark equity (realized + open MTM) onto the curve.
            mtm = self.equity
            for sym, tr in self.open_trades.items():
                px = self.close_panel.loc[ts, sym]
                if np.isfinite(px):
                    mtm += (px - tr.entry) * tr.size * tr.direction
            self.equity_curve.append((ts, mtm))

        # Force-close anything still open at the last available price.
        last_ts = self.clock[-1]
        for sym in list(self.open_trades.keys()):
            px = self.close_panel.loc[last_ts, sym]
            self._close_trade(self.open_trades[sym], last_ts, float(px), "eod")

        return BacktestResult(self)


class BacktestResult:
    def __init__(self, bt: Backtester):
        self.start_equity = bt.start_equity
        self.final_equity = bt.equity
        self.trades = bt.closed_trades
        self.blocked = bt.blocked_log
        self.equity_curve = pd.Series(
            {ts: v for ts, v in bt.equity_curve}, name="equity"
        )
        self.equity_curve.index.name = "ts"

    # ---- metrics ----
    def stats(self) -> dict:
        eq = self.equity_curve
        rets = eq.pct_change().dropna()
        total_ret = self.final_equity / self.start_equity - 1
        n = len(self.trades)
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        roll_max = eq.cummax()
        dd = (eq / roll_max - 1.0)
        # crude annualization from bar frequency of the unified clock
        if len(eq) > 2:
            dt = (eq.index[-1] - eq.index[0]).total_seconds()
            bars_per_year = len(eq) / (dt / (365 * 24 * 3600)) if dt > 0 else 0
            sharpe = (rets.mean() / rets.std() * np.sqrt(bars_per_year)
                      if rets.std() > 0 else 0.0)
        else:
            sharpe = 0.0
        return {
            "total_return_pct": round(total_ret * 100, 2),
            "final_equity": round(self.final_equity, 2),
            "n_trades": n,
            "win_rate_pct": round(100 * len(wins) / n, 1) if n else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
            "avg_r": round(np.mean([t.r_multiple for t in self.trades]), 3) if n else 0.0,
            "expectancy_$": round(np.mean([t.pnl for t in self.trades]), 2) if n else 0.0,
            "max_drawdown_pct": round(dd.min() * 100, 2),
            "sharpe_annualized": round(float(sharpe), 2),
            "blocked_by_filter": len(self.blocked),
        }

    def by_strategy(self) -> pd.DataFrame:
        rows = {}
        for t in self.trades:
            r = rows.setdefault(t.strategy, {"n": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
            r["n"] += 1
            r["wins"] += int(t.pnl > 0)
            r["pnl"] += t.pnl
            r["r"] += t.r_multiple
        df = pd.DataFrame(rows).T
        if not df.empty:
            df["win_rate_%"] = (100 * df["wins"] / df["n"]).round(1)
            df["avg_r"] = (df["r"] / df["n"]).round(3)
            df["pnl_$"] = df["pnl"].round(2)
            df = df[["n", "win_rate_%", "avg_r", "pnl_$"]]
        return df

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "symbol": t.symbol, "strategy": t.strategy,
            "dir": t.direction, "entry_ts": t.entry_ts, "exit_ts": t.exit_ts,
            "entry": round(t.entry, 4), "exit": round(t.exit, 4),
            "size": round(t.size, 4), "pnl": round(t.pnl, 2),
            "r": round(t.r_multiple, 3), "reason": t.exit_reason,
        } for t in self.trades])
