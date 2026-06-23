"""
Data layer.

Provides OHLCV bars per instrument at a given timeframe. Ships with a
synthetic generator so the whole engine runs with no API keys. To go live,
implement `load_alpaca_bars` / `load_polygon_bars` with the same return
contract and point `load_bars` at it.

Return contract for every loader:
    pd.DataFrame indexed by tz-aware UTC DatetimeIndex (name='ts'),
    columns: ['open','high','low','close','volume'], sorted ascending,
    no duplicate timestamps.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd


TIMEFRAME_MINUTES = {"15m": 15, "1h": 60, "4h": 240}

# Local cache for downloaded bars so re-runs don't re-hit the API.
CACHE_DIR = Path(__file__).resolve().parents[1] / "data_cache"

# Symbols that should be routed to Alpaca's crypto API (24/7, keyless data).
# Maps our internal ticker -> Alpaca's crypto pair notation.
CRYPTO_SYMBOLS = {"BTC": "BTC/USD", "ETH": "ETH/USD"}

# Required columns and index contract for every loader's return value.
OHLCV_COLS = ["open", "high", "low", "close", "volume"]


def _bars_per_year(timeframe: str) -> float:
    # crypto trades 24/7; equities/commodity ETFs ~6.5h/day, 252 days.
    # We approximate with calendar bars for synthetic data; real loaders
    # will carry true session gaps.
    minutes = TIMEFRAME_MINUTES[timeframe]
    return (365 * 24 * 60) / minutes


def generate_synthetic_bars(
    symbol: str,
    timeframe: str,
    n_bars: int = 4000,
    *,
    drift: float = 0.0,
    vol: float = 0.012,
    regime: str = "mixed",
    seed: int | None = None,
) -> pd.DataFrame:
    """Generate plausible OHLCV bars.

    `regime` shapes the price path so strategies have something to find:
      - 'trend'      : persistent drift (good for trend-following tests)
      - 'meanrev'    : Ornstein-Uhlenbeck pull to a level
      - 'breakout'   : low-vol coil then vol expansion bursts
      - 'mixed'      : alternating blocks of the above
    """
    rng = np.random.default_rng(seed if seed is not None else abs(hash(symbol)) % (2**32))
    freq = f"{TIMEFRAME_MINUTES[timeframe]}min"
    idx = pd.date_range("2023-01-01", periods=n_bars, freq=freq, tz="UTC", name="ts")

    rets = np.zeros(n_bars)
    if regime == "mixed":
        blocks = ["trend", "meanrev", "breakout"]
        block_len = max(50, n_bars // 12)
        sub = []
        for i in range(0, n_bars, block_len):
            r = blocks[(i // block_len) % len(blocks)]
            sub.append((i, min(i + block_len, n_bars), r))
    else:
        sub = [(0, n_bars, regime)]

    level = 0.0
    for start, end, r in sub:
        m = end - start
        if r == "trend":
            # Modest, decaying drift + noise. Keeps trends realistic instead
            # of producing smooth multi-hundred-percent synthetic runs.
            local_drift = rng.choice([-1, 1]) * vol * 0.10 + drift
            decay = np.linspace(1.0, 0.3, m)  # trends fade, don't run forever
            rets[start:end] = local_drift * decay + rng.normal(0, vol, m)
        elif r == "meanrev":
            theta, mu = 0.08, 0.0
            x = level
            out = np.empty(m)
            for k in range(m):
                x = x + theta * (mu - x) + rng.normal(0, vol, 1)[0]
                out[k] = x
            rets[start:end] = np.diff(np.concatenate([[level], out]))
            level = x
        elif r == "breakout":
            coil = rng.normal(0, vol * 0.35, m)
            n_bursts = max(1, m // 60)
            for _ in range(n_bursts):
                p = rng.integers(0, m)
                burst_len = rng.integers(3, 10)
                sign = rng.choice([-1, 1])
                coil[p:p + burst_len] += sign * vol * rng.uniform(2, 4)
            rets[start:end] = coil + drift

    price = 100 * np.exp(np.cumsum(rets))
    # Build OHLC from the close path with intrabar noise.
    close = price
    open_ = np.concatenate([[close[0]], close[:-1]])
    noise = np.abs(rng.normal(0, vol * 0.6, n_bars)) * close
    high = np.maximum(open_, close) + noise
    low = np.minimum(open_, close) - noise
    volume = rng.lognormal(mean=12, sigma=0.4, size=n_bars)

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
    return df


def generate_correlated_pair(
    base_symbol: str,
    peer_symbol: str,
    timeframe: str,
    n_bars: int = 6000,
    *,
    rho: float = 0.9,
    vol: float = 0.012,
    regime: str = "mixed",
    seed: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate two instruments whose returns are correlated at ~rho.

    Use this for SPY/QQQ (or GLD/USO) so the correlation filter is actually
    exercised in tests, mirroring real-world index co-movement.
    """
    base = generate_synthetic_bars(base_symbol, timeframe, n_bars,
                                   vol=vol, regime=regime, seed=seed)
    rng = np.random.default_rng((seed or 0) + 999)
    base_ret = np.log(base["close"] / base["close"].shift(1)).fillna(0).to_numpy()
    idio = rng.normal(0, vol, n_bars)
    peer_ret = rho * base_ret + np.sqrt(max(1e-9, 1 - rho**2)) * idio
    peer_close = 100 * np.exp(np.cumsum(peer_ret))
    open_ = np.concatenate([[peer_close[0]], peer_close[:-1]])
    noise = np.abs(rng.normal(0, vol * 0.6, n_bars)) * peer_close
    peer = pd.DataFrame({
        "open": open_,
        "high": np.maximum(open_, peer_close) + noise,
        "low": np.minimum(open_, peer_close) - noise,
        "close": peer_close,
        "volume": rng.lognormal(12, 0.4, n_bars),
    }, index=base.index)
    return base, peer


# --------------------------------------------------------------------------
# Real data — Alpaca
# --------------------------------------------------------------------------

def _normalize_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Coerce a provider frame to the documented contract and validate it.

    Contract: tz-aware UTC DatetimeIndex named 'ts', columns
    ['open','high','low','close','volume'], ascending, no duplicate ts.
    """
    out = df.copy()
    # Alpaca returns a (symbol, timestamp) MultiIndex; drop the symbol level.
    if isinstance(out.index, pd.MultiIndex):
        lvl = "timestamp" if "timestamp" in out.index.names else out.index.names[-1]
        out = out.reset_index().set_index(lvl)
    out.columns = [str(c).lower() for c in out.columns]
    missing = [c for c in OHLCV_COLS if c not in out.columns]
    if missing:
        raise ValueError(f"{symbol}: missing columns {missing}; got {list(out.columns)}")
    out = out[OHLCV_COLS]

    idx = pd.DatetimeIndex(out.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    out.index = idx
    out.index.name = "ts"

    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(subset=OHLCV_COLS)
    if out.empty:
        raise ValueError(f"{symbol}: no rows after normalization")
    return out


def _to_alpaca_timeframe(timeframe: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    n = {"15m": 15, "1h": 1, "4h": 4}[timeframe]
    unit = TimeFrameUnit.Minute if timeframe == "15m" else TimeFrameUnit.Hour
    return TimeFrame(n, unit)


def _cache_path(symbol: str, timeframe: str, feed: str, start, end) -> Path:
    safe = symbol.replace("/", "-")
    tag = f"{pd.Timestamp(start):%Y%m%d}_{pd.Timestamp(end):%Y%m%d}"
    return CACHE_DIR / f"{safe}_{timeframe}_{feed}_{tag}.parquet"


def _load_env_keys() -> tuple[str | None, str | None]:
    """Read Alpaca keys from the environment, loading a local .env if present."""
    try:
        from dotenv import load_dotenv
        load_dotenv(CACHE_DIR.parent.parent / ".env")
    except Exception:
        pass
    return os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")


def load_alpaca_bars(
    symbol: str,
    timeframe: str,
    *,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    feed: str = "iex",
    adjustment: str = "all",
    use_cache: bool = True,
    verbose: bool = True,
    **_ignored,
) -> pd.DataFrame:
    """Fetch real OHLCV bars from Alpaca, returning the documented contract.

    Equities (SPY/QQQ/GLD/USO) use the stock API on the free `iex` feed and
    require ALPACA_API_KEY / ALPACA_SECRET_KEY. Crypto (BTC) uses the keyless
    crypto API. Results are cached to local parquet; pass use_cache=False to
    force a re-fetch.

    Free intraday history is limited, so we request a wide window and report
    the ACTUAL range returned rather than silently truncating. Defaults:
    15m -> last ~2y, 1h -> last ~3y, 4h -> last ~6y (provider may return less).
    """
    end = pd.Timestamp.utcnow().tz_localize(None) if end is None else pd.Timestamp(end)
    if start is None:
        years = {"15m": 2, "1h": 3, "4h": 6}[timeframe]
        start = end - pd.DateOffset(years=years)
    else:
        start = pd.Timestamp(start)

    is_crypto = symbol in CRYPTO_SYMBOLS
    feed_tag = "crypto" if is_crypto else feed
    cache = _cache_path(symbol, timeframe, feed_tag, start, end)
    if use_cache and cache.exists():
        df = pd.read_parquet(cache)
        if verbose:
            _report_range(symbol, timeframe, df, cached=True)
        return df

    tf = _to_alpaca_timeframe(timeframe)

    if is_crypto:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        client = CryptoHistoricalDataClient()  # crypto data needs no keys
        req = CryptoBarsRequest(
            symbol_or_symbols=CRYPTO_SYMBOLS[symbol],
            timeframe=tf, start=start.to_pydatetime(), end=end.to_pydatetime(),
        )
        raw = client.get_crypto_bars(req).df
    else:
        key, secret = _load_env_keys()
        if not key or not secret:
            raise RuntimeError(
                f"{symbol}: equity data needs Alpaca keys. Set ALPACA_API_KEY and "
                "ALPACA_SECRET_KEY in your environment or a .env file at the project root."
            )
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        client = StockHistoricalDataClient(key, secret)
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=tf,
            start=start.to_pydatetime(), end=end.to_pydatetime(),
            feed=DataFeed(feed), adjustment=Adjustment(adjustment),
        )
        raw = client.get_stock_bars(req).df

    if raw is None or len(raw) == 0:
        raise ValueError(
            f"{symbol}: Alpaca returned no bars for {timeframe} "
            f"{start:%Y-%m-%d}..{end:%Y-%m-%d} (feed={feed_tag})."
        )

    df = _normalize_ohlcv(raw, symbol)
    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache)
    if verbose:
        _report_range(symbol, timeframe, df, cached=False)
    return df


def _report_range(symbol: str, timeframe: str, df: pd.DataFrame, *, cached: bool) -> None:
    src = "cache" if cached else "alpaca"
    print(
        f"  [{src}] {symbol:>4} {timeframe:>3}: {len(df):>6} bars  "
        f"{df.index[0]:%Y-%m-%d %H:%M} -> {df.index[-1]:%Y-%m-%d %H:%M} UTC"
    )


def load_bars(symbol: str, timeframe: str, source: str = "synthetic", **kwargs) -> pd.DataFrame:
    """Single entry point. `source` selects the loader; all return the same
    documented OHLCV contract so the rest of the engine is source-agnostic."""
    if source == "synthetic":
        return generate_synthetic_bars(symbol, timeframe, **kwargs)
    if source == "alpaca":
        return load_alpaca_bars(symbol, timeframe, **kwargs)
    raise NotImplementedError(
        f"source='{source}' not wired yet. Implement a loader returning the "
        "documented OHLCV contract and dispatch it here."
    )
