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
) -> list[Signal]:
    fast_ma = sma(df["close"], fast)
    slow_ma = sma(df["close"], slow)
    upper, lower = donchian(df, channel)
    a = atr(df, atr_period)
    adx_series = adx(df, adx_period) if adx_min is not None else None
    signals: list[Signal] = []

    for i in range(len(df)):
        ai = a.iloc[i]
        if pd.isna(ai) or ai <= 0 or pd.isna(slow_ma.iloc[i]):
            continue
        if pd.isna(upper.iloc[i]) or pd.isna(lower.iloc[i]):
            continue
        # Trend-strength gate: skip weak MA crosses that whipsaw.
        if adx_series is not None:
            av = adx_series.iloc[i]
            if pd.isna(av) or av < adx_min:
                continue
        up_regime = fast_ma.iloc[i] > slow_ma.iloc[i]
        down_regime = fast_ma.iloc[i] < slow_ma.iloc[i]
        high, low, close = df["high"].iloc[i], df["low"].iloc[i], df["close"].iloc[i]
        ts = df.index[i]
        if up_regime and high > upper.iloc[i]:
            stop = close - stop_atr_mult * ai
            signals.append(Signal(ts, symbol, "trend_following", +1, close, stop, ai,
                                  {"fast": float(fast_ma.iloc[i]), "slow": float(slow_ma.iloc[i])}))
        elif allow_short and down_regime and low < lower.iloc[i]:
            stop = close + stop_atr_mult * ai
            signals.append(Signal(ts, symbol, "trend_following", -1, close, stop, ai,
                                  {"fast": float(fast_ma.iloc[i]), "slow": float(slow_ma.iloc[i])}))
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
