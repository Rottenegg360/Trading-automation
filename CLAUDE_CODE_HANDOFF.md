# Claude Code Handoff — Multi-Strategy Trading System

## How to use this file
Unzip `backtest_engine.zip` into your project root, open the folder in Claude
Code, and paste this entire file as your first message. It contains everything
needed to continue: what's built, what's validated, the constraints that must
not be violated, and the prioritized work remaining. Do not re-architect what
already passes tests — extend it.

---

## 1. What this system is

A paper-first (later optionally live) algorithmic trading system across five
instruments, each with its own strategy but a SHARED portfolio-level risk layer.

| Instrument | Strategy           | Timeframe |
|------------|--------------------|-----------|
| SPY, QQQ   | Mean reversion     | 15m       |
| BTC        | Momentum breakout  | 1h        |
| GLD, USO   | Trend following    | 4h        |

Notes on instrument choice: gold/oil use ETF proxies (GLD/USO) because the
chosen broker (Alpaca) doesn't offer futures. This keeps everything on one API
for the paper phase. Revisit real futures (IBKR/Tradovate) only if going live.

### Non-negotiable risk rules (already implemented — preserve exactly)
1. **Hard 1% stop** — dollar loss if stop is hit == 1% of CURRENT equity.
   Verified in backtest: worst stop-out = −1.01R.
2. **Volatility-adjusted sizing** — size = (1% × equity) ÷ ATR-based stop
   distance. Every trade risks the same dollars regardless of instrument vol.
3. **Correlation filter** — block (or shrink) a new entry whose trailing
   returns correlate above threshold (default 0.7) with an already-open
   position. Verified: zero concurrent SPY+QQQ (they correlate ~0.97).

---

## 2. What is already built and WORKS

A complete event-driven backtest engine, validated end-to-end on synthetic data.
Architecture (all layers are data-source agnostic and independently testable):

```
backtest/
  engine/
    data.py         # synthetic + correlated-pair generators; REAL-DATA HOOK lives here
    indicators.py   # ATR, rolling z-score, Donchian, realized vol
    types.py        # Signal, Trade dataclasses (the contract between layers)
    risk.py         # RiskManager: 1% stop, vol sizing, correlation filter
    backtester.py   # unified-clock event loop + metrics (stats/by_strategy/trades_df)
  strategies/
    mean_reversion.py     # z-score stretch entry, revert-to-mean exit
    momentum_breakout.py  # Donchian break + volatility-expansion filter
    trend_following.py    # fast/slow MA regime + Donchian confirmation
  run_backtest.py   # wires all 5 instruments; CLI flags for risk/corr settings
  README.md
```

Key engine design points (do not break these):
- **Unified clock**: all instruments' bars merged onto one ascending timeline so
  the correlation filter sees a TRUE concurrent portfolio, not per-symbol silos.
- **No lookahead**: entry acts at the signal bar's close; stops/exits evaluated
  on subsequent bars' high/low. Indicators that peek (Donchian) use `.shift(1)`.
- **One position per symbol** at a time, for clean accounting.
- Metrics available on the result object: `.stats()`, `.by_strategy()`,
  `.trades_df()`, `.equity_curve` (pandas Series), `.blocked` (filter log).

### Last synthetic-data run (mechanics validation ONLY — not a perf claim)
```
total_return: +110.9%   trades: 629   win_rate: 41.7%
profit_factor: 1.40     max_drawdown: −47.3%   sharpe: 2.43
correlation-filter blocks: 634
By strategy:
  mean_reversion    496 trades  39.3% win  −0.269 avg_R   (LOSING — see task 2)
  momentum_breakout  55 trades  67.3% win  +3.506 avg_R   (carrying portfolio)
  trend_following    78 trades  38.5% win  +0.296 avg_R
```

---

## 3. THE CRITICAL NEXT STEP — real data (this is why we moved to Claude Code)

The previous environment was network-restricted and could not reach market-data
APIs. You (Claude Code) have full network access. The entire point of this
handoff is to backtest on REAL historical data.

### Task 1 — Real data loader (do this first)
In `engine/data.py` there is a `load_bars(symbol, timeframe, source=...)`
dispatcher. Implement real loaders that return the EXACT documented contract:

> pd.DataFrame indexed by tz-aware UTC DatetimeIndex (name='ts'), columns
> ['open','high','low','close','volume'], sorted ascending, no duplicate
> timestamps.

Recommended source: **Alpaca** (free tier covers SPY, QQQ, BTC; needs a free
API key in env vars `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`). For GLD/USO use
Alpaca equities too. Implement `load_alpaca_bars()` and wire it into the
dispatcher as `source="alpaca"`. Keep the synthetic generator intact as a
fallback/test fixture — do not delete it.

Constraints to handle explicitly:
- Free intraday (15m) history is often limited to ~30–60 days. Multi-year is
  usually daily. Pull the MAXIMUM each tier allows and TELL the user the actual
  date range retrieved per instrument — do not silently truncate.
- Align timezones to UTC. Equity sessions have gaps (nights/weekends); crypto is
  24/7. The unified clock already tolerates this, but verify no NaN leakage into
  the correlation panel after wiring real data.
- Cache downloaded bars to local parquet/CSV so re-runs don't re-hit the API.

### Task 2 — Fix the mean-reversion leg (it loses money)
The backtest correctly surfaced that mean reversion bleeds: the 1% ATR stop gets
chopped out before reversions complete (39% win, negative avg-R). This is a
STRATEGY problem, not an engine bug. On real data, experiment with: a wider or
volatility-conditioned stop; a regime filter so it only trades in RANGING
conditions (e.g. skip when a longer-timeframe trend is strong / ADX high); a
stricter entry z-score; or a time-stop. A/B every change against the baseline.

### Task 3 — Walk-forward / out-of-sample validation
A single backtest over one period overfits. Implement train/test splits or
walk-forward analysis so parameters tuned on one window are validated on unseen
data. Report in-sample vs out-of-sample stats separately.

---

## 4. After real-data validation (later milestones — don't start until 1–3 done)

### Live paper signal loop
Wrap the existing strategy + risk modules (reuse them UNCHANGED) in a loop that:
wakes on each instrument's bar close (15m/1h/4h), pulls latest bars, generates
signals, runs them through the SAME `RiskManager`, and submits orders to Alpaca
**paper** (`https://paper-api.alpaca.markets`). Persist open-position state to a
DB (SQLite fine) so the process can restart without losing track of positions.

Hosting: GitHub Actions cron is viable for paper (bar-close systems don't need a
persistent process), but requires the state-persistence layer above. A small VPS
(Hetzner CX22 ~$5/mo) under systemd is the cleaner long-term home for live.

### Cowork dashboard — two daily messages
Separate from the trading loop; it only READS the Alpaca account (read-only key).
- **Morning (pre-market):** overnight moves in the 5 instruments, key levels,
  economic calendar, current open positions + their stops, which strategies are
  armed given regime.
- **Night (post-close):** day P&L (realized + unrealized), per-strategy
  attribution, win/loss count, current exposure, anything the correlation filter
  blocked, drawdown vs limits.

### Going live (only after weeks of clean paper results)
Flip the Alpaca base URL from paper to live and move the loop to the VPS. This
is a CONFIG change, not a rebuild — the architecture is designed for it. Do not
do this until paper results genuinely justify it.

---

## 5. Working agreement for Claude Code
- Preserve the three risk invariants and the no-lookahead discipline above all.
- After any engine change, re-run `python -m backtest.run_backtest` and confirm
  the two invariants still hold (worst stop ≈ −1R; zero concurrent SPY+QQQ).
- Synthetic numbers validate mechanics only. Treat REAL out-of-sample results as
  the only evidence of edge. Be honest when a strategy doesn't work — the whole
  system is built to surface that, not hide it.
- This is paper-first. Nothing touches real money until explicitly decided.

## 6. Quick start
```bash
pip install pandas numpy        # plus alpaca-py once you wire real data
python -m backtest.run_backtest                  # synthetic baseline
python -m backtest.run_backtest --no-corr-filter # A/B the correlation filter
# then implement load_alpaca_bars() and add a --source alpaca path
```
