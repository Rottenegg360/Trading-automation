"""
Momentum breakout — BTC, 1h.

Enter on a break of the prior N-bar high/low, filtered by a volatility
expansion check so we skip low-energy chop. Ride with a trailing/structural
exit. Stop is ATR-based for uniform 1% sizing.
"""
from __future__ import annotations

import pandas as pd

from ..engine.indicators import adx, atr, donchian, obv, realized_vol
from ..engine.types import Signal


def generate_signals(
    df: pd.DataFrame,
    symbol: str,
    *,
    channel: int = 20,
    atr_period: int = 14,
    stop_atr_mult: float = 2.0,
    vol_period: int = 20,
    vol_expansion: float = 1.1,   # current vol must exceed this * median vol
    allow_short: bool = True,
    adx_min: float | None = None,        # only break out in a trending regime
    adx_period: int = 14,
    require_close_break: bool = False,   # close (not just wick) beyond channel
    obv_confirm: bool = False,           # require OBV trending with the break
    obv_lookback: int = 20,
) -> list[Signal]:
    upper, lower = donchian(df, channel)
    a = atr(df, atr_period)
    rv = realized_vol(df["close"], vol_period)
    rv_med = rv.rolling(vol_period * 3, min_periods=vol_period).median()
    adx_series = adx(df, adx_period) if adx_min is not None else None
    obv_series = obv(df["close"], df["volume"]) if obv_confirm else None
    signals: list[Signal] = []

    for i in range(len(df)):
        ai, ui, li = a.iloc[i], upper.iloc[i], lower.iloc[i]
        if pd.isna(ai) or ai <= 0 or pd.isna(ui) or pd.isna(li):
            continue
        if pd.isna(rv.iloc[i]) or pd.isna(rv_med.iloc[i]) or rv_med.iloc[i] <= 0:
            continue
        expanding = rv.iloc[i] >= vol_expansion * rv_med.iloc[i]
        if not expanding:
            continue
        if adx_series is not None:
            av = adx_series.iloc[i]
            if pd.isna(av) or av < adx_min:
                continue
        high, low, close = df["high"].iloc[i], df["low"].iloc[i], df["close"].iloc[i]
        ts = df.index[i]
        obv_up = obv_dn = True
        if obv_series is not None and i >= obv_lookback:
            obv_up = obv_series.iloc[i] > obv_series.iloc[i - obv_lookback]
            obv_dn = obv_series.iloc[i] < obv_series.iloc[i - obv_lookback]

        long_break = (close > ui) if require_close_break else (high > ui)
        short_break = (close < li) if require_close_break else (low < li)

        if long_break and obv_up:  # upside breakout
            stop = close - stop_atr_mult * ai
            signals.append(Signal(ts, symbol, "momentum_breakout", +1, close, stop, ai,
                                  {"channel_hi": float(ui)}))
        elif allow_short and short_break and obv_dn:  # downside breakout
            stop = close + stop_atr_mult * ai
            signals.append(Signal(ts, symbol, "momentum_breakout", -1, close, stop, ai,
                                  {"channel_lo": float(li)}))
    return signals


def should_exit(trade, bar, df, i, *, channel: int = 20,
                trail_atr: float | None = None, atr_period: int = 14) -> str | None:
    """Exit on a break back through the opposite half-channel (momentum fade),
    or an optional chandelier trailing stop. The hard 1% stop is untouched."""
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

    exit_ch = max(5, channel // 2)
    if i < exit_ch:
        return None
    if trade.direction == +1:
        recent_low = df["low"].iloc[i - exit_ch:i].min()
        if df["close"].iloc[i] < recent_low:
            return "momentum_fade"
    else:
        recent_high = df["high"].iloc[i - exit_ch:i].max()
        if df["close"].iloc[i] > recent_high:
            return "momentum_fade"
    return None
