# Multi-Strategy Backtest Engine

Event-driven, multi-instrument backtester for a 5-asset portfolio with a
shared portfolio-level risk layer.

| Instrument | Strategy           | Timeframe |
|------------|--------------------|-----------|
| SPY, QQQ   | Mean reversion     | 15m       |
| BTC        | Momentum breakout  | 1h        |
| GLD, USO   | Trend following    | 4h        |

## Risk layer (applies to every trade)
- **Hard 1% stop** — dollar loss at stop == 1% of current equity. Verified: worst stop-out ≈ −1.0R.
- **Volatility-adjusted sizing** — size = (1% × equity) ÷ ATR-based stop distance, so each trade risks the same dollars regardless of instrument vol.
- **Correlation filter** — blocks (or shrinks) a new entry whose trailing returns correlate above the threshold with an already-open position. Verified: zero concurrent SPY+QQQ.

## Run
```bash
python -m backtest.run_backtest                  # synthetic, 1% risk, 0.7 corr block
python -m backtest.run_backtest --no-corr-filter # A/B the filter's effect
python -m backtest.run_backtest --corr-action shrink

# Real data (Alpaca). Needs keys for equities; BTC is keyless. See setup below.
python -m backtest.run_backtest --source alpaca
python -m backtest.run_backtest --source alpaca --start 2021-01-01 --end 2024-12-31

# Named configs: 'baseline' (original 5-instrument) vs 'champion' (validated).
python -m backtest.run_backtest --source alpaca --config champion
```

## Configs
- **baseline** — the original handoff setup: SPY/QQQ mean-reversion, BTC momentum,
  GLD/USO trend-following, all strategy defaults.
- **champion** — the walk-forward-validated config (see `ablation.py`/`experiments.py`):
  BTC/momentum-breakout cut (confirmed out-of-sample loser), trend-following
  ADX-gated (`adx_min=20`), mean-reversion time-of-day filtered (`session_skip_bars=2`).
  Real-data result vs baseline: max drawdown −56.6% → **−23.4%**, Sharpe 0.36 → **0.68**,
  walk-forward OOS profit factor 1.07 → **1.13**, and the most-recent year flipped
  from −24% to **+21%**. Strategy defaults stay off so synthetic mechanics tests are
  unaffected; the config applies the validated params at the universe level.

## Walk-forward / out-of-sample validation
A single backtest overfits. `walkforward.py` tunes on a train window and scores
on the next unseen window, rolling forward. Only the OOS numbers are evidence
of edge.
```bash
python -m backtest.walkforward                   # synthetic, fixed params
python -m backtest.walkforward --tune            # grid-tune params per train window
python -m backtest.walkforward --source alpaca --tune --splits 5
```

## Architecture
```
engine/
  data.py         # synthetic generator + correlated-pair generator; real-data hook
  indicators.py   # ATR, z-score, Donchian, realized vol
  types.py        # Signal, Trade contracts
  risk.py         # RiskManager: 1% stop, vol sizing, correlation filter
  backtester.py   # unified-clock event loop + metrics
strategies/
  mean_reversion.py     # z-score stretch
  momentum_breakout.py  # Donchian break + vol expansion
  trend_following.py    # MA regime + Donchian confirmation
run_backtest.py   # wires all 5 instruments
```

## Real data (Alpaca) — setup
`engine/data.py` now ships a working Alpaca loader (`load_alpaca_bars`), wired
into `load_bars(... source="alpaca")`. It returns the documented OHLCV contract
(tz-aware UTC index named `ts`; open/high/low/close/volume), caches to parquet
under `data_cache/`, and prints the ACTUAL date range retrieved per instrument.

- **BTC** uses Alpaca's keyless crypto API — works out of the box.
- **SPY/QQQ/GLD/USO** use the equity API (free `iex` feed) and need keys:
  1. Create a free Alpaca account → Paper Trading → generate API keys.
  2. `cp .env.example .env` and paste `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`.
  3. `pip install -r requirements.txt` (adds alpaca-py, pyarrow, python-dotenv).

Free intraday history is limited (15m ≈ recent months; daily/4h goes back
years). The loader requests a wide window and reports what actually came back
rather than silently truncating. Strategies, risk, and the engine are
data-source agnostic — nothing else changes between synthetic and real.

## Known finding (not a bug)
Mean reversion underperforms here: the 1% ATR stop gets chopped before
reversions complete (≈39% win rate, negative avg-R). This is the engine
correctly surfacing a real strategy-tuning problem — widen/condition the
stop, add a regime filter, or trade reversion only in ranging conditions.
The synthetic generator's regimes are deliberately neutral, so these numbers
are for validating mechanics, NOT a performance claim.
