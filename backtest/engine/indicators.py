"""Vectorized indicators. Pure functions on pandas Series/DataFrames."""
from __future__ import annotations

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range. Drives volatility-based stops and sizing."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def sma(s: pd.Series, period: int) -> pd.Series:
    return s.rolling(period, min_periods=period).mean()


def rolling_z(s: pd.Series, period: int) -> pd.Series:
    """Z-score of price vs its rolling mean. Core of mean reversion."""
    mean = s.rolling(period, min_periods=period).mean()
    std = s.rolling(period, min_periods=period).std(ddof=0)
    return (s - mean) / std.replace(0, np.nan)


def donchian(df: pd.DataFrame, period: int) -> tuple[pd.Series, pd.Series]:
    """Upper/lower Donchian channel (prior-bar to avoid lookahead)."""
    upper = df["high"].rolling(period, min_periods=period).max().shift(1)
    lower = df["low"].rolling(period, min_periods=period).min().shift(1)
    return upper, lower


def realized_vol(close: pd.Series, period: int) -> pd.Series:
    """Rolling stdev of log returns — used to detect vol expansion."""
    logret = np.log(close / close.shift(1))
    return logret.rolling(period, min_periods=period).std(ddof=0)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — trend-strength gauge (0=no trend, high=strong
    trend). Used as a REGIME FILTER for mean reversion: only fade stretches when
    ADX is low (ranging), since reversion fails in strong trends. Wilder-smoothed.
    """
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    # Wilder smoothing (same alpha=1/period EWM the ATR uses).
    atr_w = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_w
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder). Overbought/oversold confirmation for
    mean reversion. <30 oversold, >70 overbought; RSI(2) is a classic
    short-horizon reversion trigger."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def bollinger_pctb(close: pd.Series, period: int = 20, n_std: float = 2.0) -> pd.Series:
    """%B: position within the Bollinger band. 0 = lower band, 1 = upper band,
    <0 / >1 = outside. A normalized 'how stretched' gauge for mean reversion."""
    mean = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper, lower = mean + n_std * std, mean - n_std * std
    return (close - lower) / (upper - lower).replace(0, np.nan)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line, signal line, histogram. Trend/momentum confirmation."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume — cumulative volume signed by price direction.
    Confirms whether a breakout is backed by participation."""
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * volume).cumsum()


def volume_delta(df: pd.DataFrame) -> pd.Series:
    """Per-bar signed volume PROXY via close-location-value. CLV in [-1,1]:
    +1 = close at high (buying pressure), -1 = close at low (selling). This is
    an APPROXIMATION of true order-flow delta (which needs trade-level aggressor
    data we don't have) — useful as a coarse buy/sell-pressure confirmation."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng
    return clv.fillna(0.0) * df["volume"]


def volume_profile(df: pd.DataFrame, lookback: int = 96, bins: int = 24,
                   value_area: float = 0.70):
    """Rolling volume profile (bar approximation). For each bar, bins the last
    `lookback` bars' closes by volume and returns (POC, VAH, VAL): the
    point-of-control price and the value-area high/low (the band holding
    `value_area` of volume). True profiles need tick data; this approximates
    from bars. Uses bars up to and including the current bar (known at close)."""
    high = df["high"].to_numpy(); low = df["low"].to_numpy()
    close = df["close"].to_numpy(); vol = df["volume"].to_numpy()
    n = len(df)
    poc = np.full(n, np.nan); vah = np.full(n, np.nan); val = np.full(n, np.nan)
    for i in range(lookback - 1, n):
        s = i - lookback + 1
        lo, hi = low[s:i + 1].min(), high[s:i + 1].max()
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            continue
        edges = np.linspace(lo, hi, bins + 1)
        hist, _ = np.histogram(close[s:i + 1], bins=edges, weights=vol[s:i + 1])
        total = hist.sum()
        if total <= 0:
            continue
        centers = (edges[:-1] + edges[1:]) / 2
        k = int(hist.argmax())
        poc[i] = centers[k]
        target = value_area * total
        lo_k = hi_k = k; acc = hist[k]
        while acc < target:
            left = hist[lo_k - 1] if lo_k - 1 >= 0 else -1.0
            right = hist[hi_k + 1] if hi_k + 1 < bins else -1.0
            if left < 0 and right < 0:
                break
            if right >= left:
                hi_k += 1; acc += hist[hi_k]
            else:
                lo_k -= 1; acc += hist[lo_k]
        val[i] = edges[lo_k]; vah[i] = edges[hi_k + 1]
    return (pd.Series(poc, index=df.index), pd.Series(vah, index=df.index),
            pd.Series(val, index=df.index))


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price, re-anchored each UTC calendar day. The
    natural intraday 'mean' for equities — fade extension from VWAP. (For
    coarse 4h bars there are too few bars/day for this to be meaningful; it's
    intended for the 15m equity legs.)"""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical * df["volume"]
    day = df.index.normalize()
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum()
    return cum_pv / cum_v.replace(0, np.nan)
