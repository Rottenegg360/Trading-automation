"""
Feature ablation: test every new indicator/filter in ISOLATION via walk-forward
OOS on real data. Each feature is a default-off toggle (baseline preserved), so
this measures each one's marginal effect against the same untouched baseline.

Discipline: we keep a feature only if it improves the target strategy's
out-of-sample avg_R without collapsing its trade count (over-filtering). More
indicators are more ways to overfit — OOS is the only verdict that counts.

Trend & mean-reversion features are tested on the 4-instrument universe
(BTC/momentum cut, our validated config). Momentum features are tested on the
full universe to answer "can any filter rescue momentum?".

Run:
    python -m backtest.ablation
"""
from __future__ import annotations

from .experiments import BASELINE, DROP_BTC
from .run_backtest import load_frames
from .walkforward import walk_forward

# Each block: (target_strategy, universe_specs, {label: feature_params})
TREND = ("trend_following", DROP_BTC, {
    "baseline": {},
    "adx_min=20": {"adx_min": 20},
    "adx_min=25": {"adx_min": 25},
    "trail_atr=3": {"trail_atr": 3.0},
    "trail_atr=4": {"trail_atr": 4.0},
    "adx20+trail3": {"adx_min": 20, "trail_atr": 3.0},
})

MEANREV = ("mean_reversion", DROP_BTC, {
    "baseline": {},
    "adx_max=25": {"adx_max": 25},
    "adx_max=20": {"adx_max": 20},
    "rsi(2)": {"rsi_confirm": True},
    "vwap": {"use_vwap": True},
    "session_skip=2": {"session_skip_bars": 2},
    "adx25+vwap+rsi": {"adx_max": 25, "use_vwap": True, "rsi_confirm": True},
})

MOMENTUM = ("momentum_breakout", BASELINE, {
    "baseline": {},
    "adx_min=25": {"adx_min": 25},
    "close_break": {"require_close_break": True},
    "obv_confirm": {"obv_confirm": True},
    "trail_atr=3": {"trail_atr": 3.0},
    "all_filters": {"adx_min": 25, "require_close_break": True,
                    "obv_confirm": True, "trail_atr": 3.0},
})


def run_block(frames, strat, specs, configs):
    print(f"\n{'=' * 86}\nFEATURE ABLATION -> {strat}  (walk-forward OOS, real data)\n{'=' * 86}")
    print(f"{'feature':<18}{'tgt OOS avgR':>14}{'tgt trades':>12}{'port OOS PF':>13}{'port OOS avgR':>15}")
    print("-" * 72)
    base_r = None
    for label, cfg in configs.items():
        wf = walk_forward(frames, n_splits=5, tune=False, risk_kwargs={},
                          specs=specs, base_params={strat: cfg} if cfg else None)
        oos = wf["oos_aggregate"]
        tgt_r = oos.get("by_strategy_avg_r", {}).get(strat, float("nan"))
        tgt_n = oos.get("by_strategy_n", {}).get(strat, 0)
        flag = ""
        if label == "baseline":
            base_r = tgt_r
        elif base_r is not None and tgt_r == tgt_r:  # not NaN
            flag = "  <-- better" if tgt_r > base_r else ""
        print(f"{label:<18}{tgt_r:>14.3f}{tgt_n:>12}{oos['profit_factor']:>13}"
              f"{oos['avg_r']:>15}{flag}")


def main():
    frames = load_frames(source="alpaca")  # cached
    for strat, specs, configs in (TREND, MEANREV, MOMENTUM):
        run_block(frames, strat, specs, configs)
    print("\nReminder: keep only features that lift the target's OOS avg_R "
          "without gutting its trade count. Everything else is overfit risk.")


if __name__ == "__main__":
    main()
