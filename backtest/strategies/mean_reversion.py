"""
Mean reversion — equities (SPY/QQQ), 15m.

Enter when price is stretched far from its rolling mean (z-score), betting
on snap-back. Exit on reversion to the mean or a stop. Stop is ATR-based so
the risk layer sizes it to the 1% budget.
"""
from __future__ import annotations

import pandas as pd

from ..engine.indicators import (adx, atr, rolling_z, rsi, session_vwap, sma,
                                 volume_delta, volume_profile)
from ..engine.types import Signal


def _session_edge_mask(index: pd.DatetimeIndex, skip_bars: int) -> pd.Series:
    """True where a bar is in the first or last `skip_bars` of its UTC session
    (calendar day). Used to avoid the open/close volatility where intraday
    reversions are noisiest."""
    if skip_bars <= 0:
        return pd.Series(False, index=index)
    day = index.normalize()
    rank = pd.Series(range(len(index)), index=index).groupby(day).cumcount()
    size = pd.Series(index.to_series().groupby(day).transform("size").values, index=index)
    return (rank < skip_bars) | (rank >= size - skip_bars)


def generate_signals(
    df: pd.DataFrame,
    symbol: str,
    *,
    z_period: int = 20,
    z_entry: float = 2.0,
    atr_period: int = 14,
    stop_atr_mult: float = 1.5,
    allow_short: bool = True,
    adx_max: float | None = None,        # only fade when ADX <= this (ranging)
    adx_period: int = 14,
    rsi_confirm: bool = False,           # require RSI oversold(long)/overbought(short)
    rsi_period: int = 2,
    rsi_os: float = 10.0,
    rsi_ob: float = 90.0,
    use_vwap: bool = False,              # also require price stretched past session VWAP
    session_skip_bars: int = 0,          # skip first/last N bars of each session
    vp_filter: bool = False,             # require price outside the volume value area
    vp_lookback: int = 96,
    vp_bins: int = 24,
    vd_confirm: bool = False,            # require volume-delta to confirm the snap-back
) -> list[Signal]:
    z = rolling_z(df["close"], z_period)
    a = atr(df, atr_period)
    mean = sma(df["close"], z_period)
    adx_series = adx(df, adx_period) if adx_max is not None else None
    rsi_series = rsi(df["close"], rsi_period) if rsi_confirm else None
    vwap_series = session_vwap(df) if use_vwap else None
    edge_mask = _session_edge_mask(df.index, session_skip_bars)
    if vp_filter:
        _poc, vah_series, val_series = volume_profile(df, vp_lookback, vp_bins)
    else:
        vah_series = val_series = None
    vd_series = volume_delta(df) if vd_confirm else None
    signals: list[Signal] = []

    for i in range(len(df)):
        zi, ai = z.iloc[i], a.iloc[i]
        if pd.isna(zi) or pd.isna(ai) or ai <= 0:
            continue
        # Regime gate: don't fade a strong trend.
        if adx_series is not None:
            av = adx_series.iloc[i]
            if pd.isna(av) or av > adx_max:
                continue
        # Time-of-day gate: avoid open/close churn.
        if edge_mask.iloc[i]:
            continue
        price = df["close"].iloc[i]
        ts = df.index[i]
        vw = vwap_series.iloc[i] if vwap_series is not None else None
        ri = rsi_series.iloc[i] if rsi_series is not None else None

        # Stretched below mean -> long; above -> short.
        if zi <= -z_entry:
            if rsi_series is not None and (pd.isna(ri) or ri > rsi_os):
                continue
            if vwap_series is not None and (pd.isna(vw) or price >= vw):
                continue
            if val_series is not None and (pd.isna(val_series.iloc[i]) or price >= val_series.iloc[i]):
                continue  # require price below the value area (genuinely stretched)
            if vd_series is not None and vd_series.iloc[i] <= 0:
                continue  # require buying pressure (snap-back starting)
            stop = price - stop_atr_mult * ai
            signals.append(Signal(ts, symbol, "mean_reversion", +1, price, stop, ai,
                                  {"z": float(zi), "target": float(mean.iloc[i])}))
        elif allow_short and zi >= z_entry:
            if rsi_series is not None and (pd.isna(ri) or ri < rsi_ob):
                continue
            if vwap_series is not None and (pd.isna(vw) or price <= vw):
                continue
            if vah_series is not None and (pd.isna(vah_series.iloc[i]) or price <= vah_series.iloc[i]):
                continue  # require price above the value area
            if vd_series is not None and vd_series.iloc[i] >= 0:
                continue  # require selling pressure
            stop = price + stop_atr_mult * ai
            signals.append(Signal(ts, symbol, "mean_reversion", -1, price, stop, ai,
                                  {"z": float(zi), "target": float(mean.iloc[i])}))
    return signals


# Exit logic the backtester calls each bar for an open trade from this strat.
def should_exit(trade, bar, df, i, *, z_period: int = 20) -> str | None:
    """Exit when price reverts through the mean (z crosses 0-ish)."""
    z = rolling_z(df["close"], z_period).iloc[i]
    if pd.isna(z):
        return None
    if trade.direction == +1 and z >= 0:
        return "reverted"
    if trade.direction == -1 and z <= 0:
        return "reverted"
    return None
