"""
End-to-end backtest across all five instruments.

    SPY, QQQ  -> mean reversion, 15m
    BTC       -> momentum breakout, 1h
    GLD, USO  -> trend following, 4h

Run:  python -m backtest.run_backtest
"""
from __future__ import annotations

import argparse
import inspect

import pandas as pd

from .engine.backtester import Backtester, Instrument
from .engine.data import generate_correlated_pair, load_bars
from .engine.risk import RiskManager
from .strategies import mean_reversion, momentum_breakout, trend_following

pd.set_option("display.width", 120)
pd.set_option("display.max_columns", 20)


# (symbol, timeframe, strategy_module, n_bars, synthetic regime hint)
UNIVERSE = [
    ("SPY", "15m", mean_reversion, 6000, "mixed"),
    ("QQQ", "15m", mean_reversion, 6000, "mixed"),
    ("BTC", "1h",  momentum_breakout, 4000, "breakout"),
    ("GLD", "4h",  trend_following, 2500, "trend"),
    ("USO", "4h",  trend_following, 2500, "trend"),
]


SPECS = [
    ("SPY", "15m", mean_reversion), ("QQQ", "15m", mean_reversion),
    ("BTC", "1h", momentum_breakout),
    ("GLD", "4h", trend_following), ("USO", "4h", trend_following),
]

# --- Named configurations -------------------------------------------------
# "baseline" = the original 5-instrument handoff setup (defaults everywhere).
# "champion" = the walk-forward-validated config: BTC/momentum cut (confirmed
#   OOS loser), trend-following ADX-gated, mean-reversion time-of-day filtered.
#   Real-data OOS: PF 1.07->1.13, avg_R 0.038->0.086, and the most-recent year
#   flipped from -24% to +21%. See backtest/ablation.py for the evidence.
CHAMPION_SPECS = [(s, tf, m) for s, tf, m in SPECS if s != "BTC"]
CHAMPION_PARAMS = {
    "trend_following": {"adx_min": 20},
    "mean_reversion": {"session_skip_bars": 2},
}

# "champion-plus" extends the trend leg with screened, OOS-positive ETFs
# (DBC/DBA/UUP) — see backtest/extend_trend.py. Walk-forward OOS PF 1.13 -> 1.19.
CHAMPION_PLUS_SPECS = CHAMPION_SPECS + [
    ("DBC", "4h", trend_following),
    ("DBA", "4h", trend_following),
    ("UUP", "4h", trend_following),
]

CONFIGS = {
    "baseline": {"specs": SPECS, "params": {}},
    "champion": {"specs": CHAMPION_SPECS, "params": CHAMPION_PARAMS},
    "champion-plus": {"specs": CHAMPION_PLUS_SPECS, "params": CHAMPION_PARAMS},
}


def _synthetic_frames(seed: int) -> dict[str, pd.DataFrame]:
    """SPY/QQQ are generated as a correlated pair (~0.9), as are GLD/USO
    (~0.6), so the correlation filter is genuinely exercised. BTC stands
    alone. This is the mechanics-validation fixture (no edge claim)."""
    spy, qqq = generate_correlated_pair("SPY", "QQQ", "15m", 6000, rho=0.92,
                                        regime="mixed", seed=seed)
    gld, uso = generate_correlated_pair("GLD", "USO", "4h", 2500, rho=0.6,
                                        regime="trend", seed=seed + 50)
    btc = load_bars("BTC", "1h", source="synthetic", n_bars=4000,
                    regime="breakout", seed=seed + 99)
    return {"SPY": spy, "QQQ": qqq, "GLD": gld, "USO": uso, "BTC": btc}


def _alpaca_frames(specs=None, start=None, end=None) -> dict[str, pd.DataFrame]:
    """Pull each instrument's REAL bars independently from Alpaca. Each loader
    reports the actual date range retrieved (see data.load_alpaca_bars)."""
    print("\n=== LOADING REAL DATA (Alpaca) ===")
    frames: dict[str, pd.DataFrame] = {}
    for sym, tf, _mod in (specs or SPECS):
        frames[sym] = load_bars(sym, tf, source="alpaca", start=start, end=end)
    return frames


def load_frames(source: str = "synthetic", seed: int = 7, start=None, end=None, specs=None):
    """Return {symbol: OHLCV df} for the configured universe (or `specs`)."""
    if source == "synthetic":
        return _synthetic_frames(seed)
    if source == "alpaca":
        return _alpaca_frames(specs=specs, start=start, end=end)
    raise ValueError(f"unknown source '{source}'")


def _accepted_kwargs(fn, params: dict) -> dict:
    """Filter `params` to only the keyword args `fn` actually accepts, so a
    single per-strategy param dict can feed both generate_signals and
    should_exit without passing unexpected kwargs to either."""
    sig = inspect.signature(fn)
    return {k: v for k, v in params.items() if k in sig.parameters}


def build_instruments_from_frames(frames: dict, params: dict | None = None,
                                  specs: list | None = None) -> list[Instrument]:
    """Wire frames + strategies into Instruments. `params` is an optional
    {strategy_name: {kwarg: value}} map used to re-parameterize strategies
    (the walk-forward optimizer relies on this). `specs` overrides the default
    (symbol, timeframe, strategy_module) universe — used to A/B strategy swaps."""
    params = params or {}
    instruments = []
    for sym, tf, mod in (specs or SPECS):
        df = frames.get(sym)
        if df is None or len(df) == 0:
            continue  # instrument absent in this window (e.g. a walk-forward slice)
        strat_name = mod.__name__.rsplit(".", 1)[-1]
        p = params.get(strat_name, {})
        sigs = mod.generate_signals(df, sym, **_accepted_kwargs(mod.generate_signals, p))
        instruments.append(Instrument(
            symbol=sym, timeframe=tf, df=df, signals=sigs,
            exit_fn=mod.should_exit,
            exit_kwargs=_accepted_kwargs(mod.should_exit, p),
        ))
    return instruments


def build_instruments(source: str = "synthetic", seed: int = 7,
                      start=None, end=None, config: str = "baseline") -> list[Instrument]:
    """Wire instruments + strategies into the engine. `source` selects synthetic
    fixtures vs real Alpaca data; `config` selects the universe + strategy params
    ('baseline' = original 5-instrument, 'champion' = validated 4-instrument)."""
    cfg = CONFIGS[config]
    frames = load_frames(source=source, seed=seed, start=start, end=end, specs=cfg["specs"])
    return build_instruments_from_frames(frames, params=cfg["params"], specs=cfg["specs"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=100_000)
    ap.add_argument("--risk-pct", type=float, default=0.01)
    ap.add_argument("--corr-threshold", type=float, default=0.7)
    ap.add_argument("--corr-action", choices=["block", "shrink"], default="block")
    ap.add_argument("--no-corr-filter", action="store_true",
                    help="disable correlation filter to A/B its effect")
    ap.add_argument("--source", choices=["synthetic", "alpaca"], default="synthetic",
                    help="bar source: synthetic fixture or real Alpaca data")
    ap.add_argument("--start", default=None, help="UTC start date for real data, e.g. 2021-01-01")
    ap.add_argument("--end", default=None, help="UTC end date for real data, e.g. 2024-12-31")
    ap.add_argument("--config", choices=list(CONFIGS), default="baseline",
                    help="'baseline' = original 5-instrument; 'champion' = validated 4-instrument")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    print(f"\n[config: {args.config} | source: {args.source}]")
    instruments = build_instruments(source=args.source, seed=args.seed,
                                    start=args.start, end=args.end, config=args.config)

    risk = RiskManager(
        risk_pct=args.risk_pct,
        corr_threshold=2.0 if args.no_corr_filter else args.corr_threshold,
        corr_action=args.corr_action,
    )
    bt = Backtester(instruments, risk, starting_equity=args.equity)
    result = bt.run()

    print("\n=== PORTFOLIO STATS ===")
    for k, v in result.stats().items():
        print(f"  {k:>22}: {v}")

    print("\n=== BY STRATEGY ===")
    print(result.by_strategy())

    if result.blocked:
        print(f"\n=== CORRELATION FILTER blocked {len(result.blocked)} entries (first 5) ===")
        for b in result.blocked[:5]:
            print("  ", b)

    return result


if __name__ == "__main__":
    main()
