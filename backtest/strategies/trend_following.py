"""
Trend following — gold/oil ETFs (GLD/USO), 4h.

Enter in the direction of an established regime (fast vs slow MA), confirmed
by a Donchian breakout. Hold while the regime persists. ATR-based stop.
"""
from __future__ import annotations

import pandas as pd

from ..engine.indicators import adx, atr, donchian, sma
from ..engine.types import Signal


def generate_signals(
    df: pd.DataFrame,
    symbol: str,
    *,
    fast: int = 20,
    slow: int = 50,
    channel: int = 20,
    atr_period: int = 14,
    stop_atr_mult: float = 3.0,
    allow_short: bool = True,
    adx_min: float | None = None,    # if set, require ADX >= this (real trend only)
    adx_period: int = 14,
    stop_entry: bool = False,        # fill at the breakout LEVEL intra-bar, not the close
) -> list[Signal]:
    fast_ma = sma(df["close"], fast)
    slow_ma = sma(df["close"], slow)
    upper, lower = donchian(df, channel)  # already prior-bar (shift(1)); known pre-bar
    a = atr(df, atr_period)
    adx_series = adx(df, adx_period) if adx_min is not None else None
    signals: list[Signal] = []

    for i in range(len(df)):
        # In stop_entry mode the order rests BEFORE bar i, so everything that
        # arms it must be known at the prior bar's close (j = i-1) — otherwise
        # we'd be using bar i's close to justify an intra-bar i fill (lookahead).
        j = i - 1 if stop_entry else i
        if j < 0:
            continue
        ai = a.iloc[j]
        if pd.isna(ai) or ai <= 0 or pd.isna(slow_ma.iloc[j]):
            continue
        up_lvl, dn_lvl = upper.iloc[i], lower.iloc[i]
        if pd.isna(up_lvl) or pd.isna(dn_lvl):
            continue
        if adx_series is not None:
            av = adx_series.iloc[j]
            if pd.isna(av) or av < adx_min:
                continue
        up_regime = fast_ma.iloc[j] > slow_ma.iloc[j]
        down_regime = fast_ma.iloc[j] < slow_ma.iloc[j]
        high, low, close, open_ = (df["high"].iloc[i], df["low"].iloc[i],
                                   df["close"].iloc[i], df["open"].iloc[i])
        ts = df.index[i]
        meta = {"fast": float(fast_ma.iloc[j]), "slow": float(slow_ma.iloc[j])}
        if up_regime and high > up_lvl:
            # Buy-stop at the channel level; gap-ups fill at the open.
            entry = (open_ if open_ > up_lvl else up_lvl) if stop_entry else close
            stop = entry - stop_atr_mult * ai
            signals.append(Signal(ts, symbol, "trend_following", +1, entry, stop, ai, meta))
        elif allow_short and down_regime and low < dn_lvl:
            entry = (open_ if open_ < dn_lvl else dn_lvl) if stop_entry else close
            stop = entry + stop_atr_mult * ai
            signals.append(Signal(ts, symbol, "trend_following", -1, entry, stop, ai, meta))
    return signals


def should_exit(trade, bar, df, i, *, fast: int = 20, slow: int = 50,
                trail_atr: float | None = None, atr_period: int = 14) -> str | None:
    """Exit on MA regime flip, or (optionally) a chandelier trailing stop that
    locks in trend gains before the slower MA flips. The hard 1% stop set at
    entry is untouched — this only adds an EARLIER discretionary exit."""
    # Chandelier trailing exit (optional): highest-high (long) / lowest-low
    # (short) since entry, minus/plus k*ATR. Lets winners run but caps give-back.
    if trail_atr is not None:
        j = df.index.get_indexer([trade.entry_ts])[0]
        if j != -1 and i > j:
            a = atr(df, atr_period).iloc[i]
            if pd.notna(a) and a > 0:
                if trade.direction == +1:
                    hh = df["high"].iloc[j:i + 1].max()
                    if df["close"].iloc[i] < hh - trail_atr * a:
                        return "trail_stop"
                else:
                    ll = df["low"].iloc[j:i + 1].min()
                    if df["close"].iloc[i] > ll + trail_atr * a:
                        return "trail_stop"

    fast_ma = sma(df["close"], fast).iloc[i]
    slow_ma = sma(df["close"], slow).iloc[i]
    if pd.isna(fast_ma) or pd.isna(slow_ma):
        return None
    if trade.direction == +1 and fast_ma < slow_ma:
        return "regime_flip"
    if trade.direction == -1 and fast_ma > slow_ma:
        return "regime_flip"
    return None
