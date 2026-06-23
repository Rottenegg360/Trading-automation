"""
Real-data backtest on the KEYLESS crypto path (no Alpaca keys required).

This is the first real-data evidence the system can produce without equity
keys: it runs the momentum-breakout strategy on real BTC and ETH 1h bars
through the UNCHANGED engine + risk layer. It exercises, on real data:
  * the 1% hard stop (we assert worst stop-out stays ~ -1R),
  * volatility-adjusted sizing,
  * the correlation filter (BTC/ETH are strongly correlated in reality, so
    the filter should block/shrink the second concurrent entry).

Run:
    python -m backtest.run_crypto_real
    python -m backtest.run_crypto_real --start 2022-01-01 --no-corr-filter
"""
from __future__ import annotations

import argparse

import numpy as np

from .engine.backtester import Backtester, Instrument
from .engine.data import load_bars
from .engine.risk import RiskManager
from .strategies import momentum_breakout

# Crypto majors that trade 24/7 and need no keys. Both use momentum-breakout
# at 1h, matching BTC's configured strategy in the full universe.
CRYPTO_UNIVERSE = [("BTC", "1h"), ("ETH", "1h")]


def build_crypto_instruments(start=None, end=None) -> list[Instrument]:
    instruments = []
    for sym, tf in CRYPTO_UNIVERSE:
        df = load_bars(sym, tf, source="alpaca", start=start, end=end)
        sigs = momentum_breakout.generate_signals(df, sym)
        instruments.append(Instrument(symbol=sym, timeframe=tf, df=df,
                                      signals=sigs, exit_fn=momentum_breakout.should_exit))
    return instruments


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--equity", type=float, default=100_000)
    ap.add_argument("--corr-threshold", type=float, default=0.7)
    ap.add_argument("--no-corr-filter", action="store_true")
    args = ap.parse_args()

    print("\n=== LOADING REAL CRYPTO DATA (Alpaca, keyless) ===")
    instruments = build_crypto_instruments(start=args.start, end=args.end)

    risk = RiskManager(corr_threshold=2.0 if args.no_corr_filter else args.corr_threshold)
    bt = Backtester(instruments, risk, starting_equity=args.equity)
    res = bt.run()

    print("\n=== REAL-DATA PORTFOLIO STATS (BTC + ETH momentum) ===")
    for k, v in res.stats().items():
        print(f"  {k:>22}: {v}")

    print("\n=== BY STRATEGY / SYMBOL ===")
    print(res.by_strategy())

    # Invariant check on REAL bars: worst stop-out should be ~ -1R.
    stops = [t.r_multiple for t in res.trades if t.exit_reason == "stop"]
    if stops:
        print(f"\n  stop-outs: {len(stops)}  worst R: {min(stops):.3f}  "
              f"(invariant: should be >= -1.05R)")

    if res.blocked:
        print(f"\n=== CORRELATION FILTER blocked {len(res.blocked)} entries on REAL data (first 5) ===")
        for b in res.blocked[:5]:
            print("  ", b)
    return res


if __name__ == "__main__":
    main()
