"""
Extend trend-following: trend-following is the one OOS-robust edge, so give it
more surface area. This screens a diversified basket of 4h ETF candidates,
each run STANDALONE through walk-forward OOS with the champion's ADX gate
(adx_min=20), and ranks them by out-of-sample avg_R.

Only instruments that carry a genuine OOS edge (positive avg_R, enough trades)
should be promoted into the portfolio — adding losers just dilutes the edge,
exactly as momentum did.

Candidates span uncorrelated trend sources (rates / metals / broad commodities /
natgas / USD / ag) so additions diversify rather than double up on GLD/USO.

Run:
    python -m backtest.extend_trend
"""
from __future__ import annotations

import pandas as pd

from .engine.backtester import Backtester
from .engine.data import load_bars
from .engine.risk import RiskManager
from .run_backtest import build_instruments_from_frames
from .strategies import trend_following
from .walkforward import walk_forward

TF = "4h"
# Incumbents (reference) + diversified candidates.
INCUMBENTS = ["GLD", "USO"]
CANDIDATES = ["TLT", "SLV", "DBC", "UNG", "UUP", "DBA"]
ADX_PARAMS = {"trend_following": {"adx_min": 20}}


def screen_one(sym: str) -> dict | None:
    try:
        df = load_bars(sym, TF, source="alpaca")  # default wide window, cached
    except Exception as e:
        print(f"  {sym:>4}: SKIP ({type(e).__name__}: {str(e)[:60]})")
        return None
    frames = {sym: df}
    specs = [(sym, TF, trend_following)]

    # Full-period (context).
    ins = build_instruments_from_frames(frames, params=ADX_PARAMS, specs=specs)
    full = Backtester(ins, RiskManager(), starting_equity=100_000).run().stats()

    # Walk-forward OOS (the real test).
    wf = walk_forward(frames, n_splits=5, tune=False, risk_kwargs={},
                      specs=specs, base_params=ADX_PARAMS)
    oos = wf["oos_aggregate"]
    return {
        "sym": sym,
        "bars": len(df),
        "from": f"{df.index[0]:%Y-%m}",
        "full_ret%": full["total_return_pct"],
        "full_avgR": full["avg_r"],
        "OOS_avgR": oos.get("avg_r", float("nan")),
        "OOS_PF": oos.get("profit_factor", float("nan")),
        "OOS_n": oos.get("n_trades", 0),
    }


def main():
    print("\n=== SCREENING TREND CANDIDATES (4h, adx_min=20, walk-forward OOS) ===")
    rows = []
    for sym in INCUMBENTS + CANDIDATES:
        r = screen_one(sym)
        if r:
            rows.append(r)
            tag = "(incumbent)" if sym in INCUMBENTS else ""
            print(f"  {sym:>4}: OOS avgR {r['OOS_avgR']:+.3f}  PF {r['OOS_PF']:.2f}  "
                  f"n={r['OOS_n']:<4} full {r['full_ret%']:+.1f}%  "
                  f"[{r['bars']} bars from {r['from']}] {tag}")

    df = pd.DataFrame(rows).sort_values("OOS_avgR", ascending=False)
    print("\n=== RANKED BY OUT-OF-SAMPLE avg_R ===")
    print(df.to_string(index=False))

    winners = df[(df["OOS_avgR"] > 0.05) & (df["OOS_n"] >= 20)]
    print("\n=== PROMOTE (OOS avgR > 0.05 and >= 20 trades) ===")
    print("  " + ", ".join(winners["sym"]) if len(winners) else "  (none cleared the bar)")
    return df


if __name__ == "__main__":
    main()
