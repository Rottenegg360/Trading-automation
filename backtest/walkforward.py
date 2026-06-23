"""
Walk-forward / out-of-sample validation  (Task 3).

A single backtest over one period overfits: you can always find parameters
that look great in hindsight. Walk-forward analysis guards against that by
TUNING on one window and SCORING on the next, unseen window — then rolling
forward. Only the out-of-sample (OOS) numbers are evidence of edge.

Design
------
* Operates on the same {symbol: OHLCV df} frames the live backtest uses, so
  synthetic and real (Alpaca) data run through the identical path.
* Rolling folds over the unified calendar timeline. Each fold has a train
  window and the immediately-following test window (no overlap, no peeking).
* Per-strategy parameter tuning is OPTIONAL. When a param grid is supplied,
  each strategy is tuned INDEPENDENTLY on the train window (coordinate-wise:
  other strategies held at defaults), the best combo by the chosen metric is
  selected, and that combo is then scored untouched on the test window.
* Reports in-sample vs out-of-sample stats per fold and aggregated, plus the
  parameters chosen each fold (parameter stability is itself a signal).

Caveat (documented, not hidden): independent per-strategy tuning ignores
cross-strategy interaction through the shared correlation filter. It is a
tractable, honest v1; joint optimization can come later if OOS warrants it.

Run:
    python -m backtest.walkforward                      # synthetic, no tuning
    python -m backtest.walkforward --tune               # synthetic, tune grids
    python -m backtest.walkforward --source alpaca --tune
"""
from __future__ import annotations

import argparse
import itertools

import numpy as np
import pandas as pd

from .engine.backtester import Backtester
from .engine.risk import RiskManager
from .run_backtest import SPECS, build_instruments_from_frames, load_frames

pd.set_option("display.width", 140)
pd.set_option("display.max_columns", 30)


# Default tuning grids. Mean reversion gets the most attention because the
# baseline flagged it as the losing leg (Task 2). Keep grids small so the
# walk-forward stays tractable; widen once real data justifies it.
DEFAULT_GRIDS: dict[str, dict[str, list]] = {
    "mean_reversion": {
        "z_entry": [2.0, 2.5, 3.0],
        "stop_atr_mult": [1.5, 2.5, 3.5],
    },
    "momentum_breakout": {
        "channel": [20, 40],
        "stop_atr_mult": [2.0, 3.0],
    },
    "trend_following": {
        "channel": [20, 40],
        "stop_atr_mult": [3.0, 4.0],
    },
}


def slice_frames(frames: dict, start, end) -> dict:
    """Restrict every instrument's bars to [start, end). Indicators are
    recomputed inside this window only — no information leaks from the future."""
    return {s: df.loc[(df.index >= start) & (df.index < end)] for s, df in frames.items()}


def _grid_combos(grid: dict[str, list]) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]


def run_on_frames(frames: dict, params: dict | None, risk_kwargs: dict,
                  equity: float, specs: list | None = None) -> "object":
    instruments = build_instruments_from_frames(frames, params, specs=specs)
    bt = Backtester(instruments, RiskManager(**risk_kwargs), starting_equity=equity)
    return bt.run()


def _metric_value(stats_like: dict, metric: str) -> float:
    v = stats_like.get(metric, float("-inf"))
    try:
        v = float(v)
    except (TypeError, ValueError):
        return float("-inf")
    return v if np.isfinite(v) else float("-inf")


def _strategy_train_metric(result, strat_name: str, metric: str) -> tuple[float, int]:
    """Per-strategy metric from a portfolio result, used to tune one strategy
    in isolation. Returns (metric_value, n_trades)."""
    bs = result.by_strategy()
    if strat_name not in bs.index:
        return float("-inf"), 0
    row = bs.loc[strat_name]
    n = int(row["n"])
    key = {"avg_r": "avg_r", "win_rate": "win_rate_%", "pnl": "pnl_$"}.get(metric, "avg_r")
    val = float(row[key]) if key in row else float("-inf")
    return (val if np.isfinite(val) else float("-inf")), n


def tune_on_train(train_frames: dict, grids: dict, risk_kwargs: dict,
                  equity: float, metric: str, min_trades: int,
                  specs: list | None = None) -> dict:
    """Independently tune each strategy on the train window. A param combo is
    only eligible if it produced at least `min_trades` (avoids picking a combo
    that looks great off two lucky trades)."""
    chosen: dict[str, dict] = {}
    strat_names = {mod.__name__.rsplit(".", 1)[-1] for _, _, mod in (specs or SPECS)}
    for strat in strat_names:
        grid = grids.get(strat)
        if not grid:
            continue
        best_combo, best_score = {}, float("-inf")
        for combo in _grid_combos(grid):
            res = run_on_frames(train_frames, {strat: combo}, risk_kwargs, equity, specs=specs)
            score, n = _strategy_train_metric(res, strat, metric)
            if n < min_trades:
                continue
            if score > best_score:
                best_score, best_combo = score, combo
        if best_combo:
            chosen[strat] = best_combo
    return chosen


def walk_forward(
    frames: dict,
    *,
    n_splits: int = 4,
    train_frac: float = 0.6,
    tune: bool = False,
    grids: dict | None = None,
    metric: str = "avg_r",
    min_trades: int = 10,
    risk_kwargs: dict | None = None,
    equity: float = 100_000.0,
    specs: list | None = None,
    base_params: dict | None = None,
) -> dict:
    """Rolling walk-forward over the unified timeline.

    The calendar span is divided into `n_splits` rolling steps. Each step uses
    a leading `train_frac` slice to tune (if enabled) and the trailing slice to
    score out-of-sample. Returns a dict with per-fold and aggregated IS/OOS
    stats, plus the parameters chosen each fold.
    """
    risk_kwargs = risk_kwargs or {}
    grids = grids if grids is not None else DEFAULT_GRIDS

    timeline = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in frames.values()])))
    if len(timeline) < 100:
        raise ValueError("not enough bars for walk-forward")

    t0, t1 = timeline[0], timeline[-1]
    total = (t1 - t0)
    step = total / (n_splits + 1)          # each fold advances by one step
    train_span = step * (train_frac / (1 - train_frac))

    folds = []
    for k in range(n_splits):
        test_start = t0 + step * (k + 1)
        test_end = test_start + step
        train_start = max(t0, test_start - train_span)
        if test_end > t1:
            test_end = t1
        folds.append((train_start, test_start, test_end))

    fold_rows = []
    oos_results, is_results = [], []
    for k, (tr_s, te_s, te_e) in enumerate(folds, 1):
        train_frames = slice_frames(frames, tr_s, te_s)
        test_frames = slice_frames(frames, te_s, te_e)
        # Instruments have different history lengths (15m intraday is short,
        # 4h spans years). Keep only those with enough bars in BOTH windows;
        # don't discard the whole fold because one instrument is absent.
        keep = {s for s in frames
                if len(train_frames.get(s, [])) >= 60 and len(test_frames.get(s, [])) >= 60}
        if not keep:
            continue
        train_frames = {s: train_frames[s] for s in keep}
        test_frames = {s: test_frames[s] for s in keep}

        params = tune_on_train(train_frames, grids, risk_kwargs, equity, metric, min_trades, specs=specs) if tune else {}
        # Merge fixed feature params (ablation) under any tuned params.
        if base_params:
            merged = {k: dict(v) for k, v in base_params.items()}
            for k, v in params.items():
                merged.setdefault(k, {}).update(v)
            params = merged

        is_res = run_on_frames(train_frames, params, risk_kwargs, equity, specs=specs)
        oos_res = run_on_frames(test_frames, params, risk_kwargs, equity, specs=specs)
        is_results.append(is_res)
        oos_results.append(oos_res)

        is_s, oos_s = is_res.stats(), oos_res.stats()
        fold_rows.append({
            "fold": k,
            "train": f"{tr_s:%Y-%m-%d}->{te_s:%Y-%m-%d}",
            "test": f"{te_s:%Y-%m-%d}->{te_e:%Y-%m-%d}",
            "IS_avg_r": is_s["avg_r"], "OOS_avg_r": oos_s["avg_r"],
            "IS_pf": is_s["profit_factor"], "OOS_pf": oos_s["profit_factor"],
            "IS_ret%": is_s["total_return_pct"], "OOS_ret%": oos_s["total_return_pct"],
            "OOS_trades": oos_s["n_trades"],
            "params": params or "(defaults)",
        })

    report = {
        "folds": pd.DataFrame(fold_rows),
        "oos_aggregate": _aggregate(oos_results),
        "is_aggregate": _aggregate(is_results),
    }
    return report


def _aggregate(results: list) -> dict:
    """Pool all trades across folds and recompute headline stats. Pooling OOS
    trades is the honest summary: it answers 'across every unseen window, did
    the system make money per unit of risk?'"""
    trades = [t for r in results for t in r.trades]
    if not trades:
        return {"n_trades": 0}
    rs = [t.r_multiple for t in trades]
    pnl = [t.pnl for t in trades]
    wins = [t for t in trades if t.pnl > 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in trades if t.pnl <= 0)
    by_strat: dict[str, list] = {}
    for t in trades:
        by_strat.setdefault(t.strategy, []).append(t.r_multiple)
    return {
        "n_trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1),
        "avg_r": round(float(np.mean(rs)), 3),
        "expectancy_$": round(float(np.mean(pnl)), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "by_strategy_avg_r": {s: round(float(np.mean(v)), 3) for s, v in by_strat.items()},
        "by_strategy_n": {s: len(v) for s, v in by_strat.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["synthetic", "alpaca"], default="synthetic")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--splits", type=int, default=4)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--tune", action="store_true", help="grid-tune params per train window")
    ap.add_argument("--metric", default="avg_r", choices=["avg_r", "win_rate", "pnl"])
    ap.add_argument("--corr-threshold", type=float, default=0.7)
    ap.add_argument("--equity", type=float, default=100_000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    frames = load_frames(source=args.source, seed=args.seed, start=args.start, end=args.end)
    report = walk_forward(
        frames, n_splits=args.splits, train_frac=args.train_frac,
        tune=args.tune, metric=args.metric,
        risk_kwargs={"corr_threshold": args.corr_threshold}, equity=args.equity,
    )

    print(f"\n=== WALK-FORWARD ({args.source}, tune={args.tune}, metric={args.metric}) ===")
    print(report["folds"].to_string(index=False))
    print("\n--- IN-SAMPLE aggregate (pooled train trades) ---")
    for k, v in report["is_aggregate"].items():
        print(f"  {k:>20}: {v}")
    print("\n--- OUT-OF-SAMPLE aggregate (pooled unseen trades -- the real test) ---")
    for k, v in report["oos_aggregate"].items():
        print(f"  {k:>20}: {v}")
    return report


if __name__ == "__main__":
    main()
