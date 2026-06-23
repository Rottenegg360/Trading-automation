"""
A/B experiment: momentum-breakout is a confirmed out-of-sample loser. Does
removing/replacing it lift the whole portfolio?

Walk-forward OOS on real data ranked the legs:
    trend_following  +0.616 avg_R  (the only OOS-robust edge)
    mean_reversion   +0.023 avg_R  (break-even)
    momentum_breakout -0.028 avg_R (loser, every test)

Variants compared (all on real Alpaca data, same risk layer, walk-forward OOS):
    A  baseline           BTC -> momentum_breakout  (current 5-instrument setup)
    B  BTC -> trend        BTC -> trend_following    (swap the loser for the winner)
    C  drop BTC            4 instruments             (isolate momentum's removal)

Run:
    python -m backtest.experiments
"""
from __future__ import annotations

from .run_backtest import SPECS, build_instruments_from_frames, load_frames
from .engine.backtester import Backtester
from .engine.risk import RiskManager
from .strategies import trend_following
from .walkforward import walk_forward

# Variant specs derived from the default universe.
BASELINE = SPECS
BTC_TREND = [(s, tf, trend_following if s == "BTC" else m) for s, tf, m in SPECS]
DROP_BTC = [(s, tf, m) for s, tf, m in SPECS if s != "BTC"]

VARIANTS = {
    "A baseline (BTC=momentum)": BASELINE,
    "B BTC=trend_following": BTC_TREND,
    "C drop BTC (4 instruments)": DROP_BTC,
}


def full_backtest(frames, specs, equity=100_000.0):
    ins = build_instruments_from_frames(frames, specs=specs)
    return Backtester(ins, RiskManager(), starting_equity=equity).run()


def main():
    frames = load_frames(source="alpaca")  # uses cache

    print("\n" + "=" * 78)
    print("A/B: replace the confirmed OOS loser (momentum_breakout). Real data.")
    print("=" * 78)

    header = f"{'variant':<28}{'FULL ret%':>10}{'FULL PF':>9}{'FULL avgR':>11}{'OOS PF':>8}{'OOS avgR':>10}{'OOS trades':>12}"
    print("\n" + header)
    print("-" * len(header))

    details = {}
    for name, specs in VARIANTS.items():
        full = full_backtest(frames, specs).stats()
        wf = walk_forward(frames, n_splits=5, tune=False,
                          risk_kwargs={}, specs=specs)
        oos = wf["oos_aggregate"]
        details[name] = (full, oos, wf)
        print(f"{name:<28}{full['total_return_pct']:>10}{full['profit_factor']:>9}"
              f"{full['avg_r']:>11}{oos['profit_factor']:>8}{oos['avg_r']:>10}"
              f"{oos['n_trades']:>12}")

    print("\n--- OUT-OF-SAMPLE avg_R by strategy (the real test) ---")
    for name, (_full, oos, _wf) in details.items():
        print(f"  {name}")
        for strat, r in oos.get("by_strategy_avg_r", {}).items():
            print(f"      {strat:<20} {r:+.3f}")

    # Verdict line: did removing momentum beat baseline OOS?
    base_oos = details["A baseline (BTC=momentum)"][1]
    print("\n--- VERDICT (OOS profit factor vs baseline) ---")
    for name, (_full, oos, _wf) in details.items():
        if name.startswith("A "):
            continue
        delta = oos["profit_factor"] - base_oos["profit_factor"]
        verdict = "BETTER" if delta > 0 else "worse"
        print(f"  {name:<28} OOS PF {oos['profit_factor']:.2f} vs {base_oos['profit_factor']:.2f} "
              f"baseline  ({delta:+.2f}, {verdict})")


if __name__ == "__main__":
    main()
