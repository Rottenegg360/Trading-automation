# Deployment — independent runtime + Cowork briefings

The trading runs **independently on GitHub Actions**. Cowork is only your
dashboard: one scheduled briefing at 9am Bangkok. They share state through the
git repo, so the Cowork side needs no API keys.

```
GitHub Actions (has Alpaca keys as secrets)
  ├─ paper-trade.yml     every ~15 min  → run --once → commit paper_state.db
  └─ daily-briefing.yml  02:00 UTC (9am ICT) → report --daily → commit briefings/latest.md
                                   │
                                   ▼
Cowork routine (no keys)   9am ICT → git pull → show briefings/latest.md
```

## One-time setup

1. **Create a GitHub repo and push this project.**
   ```bash
   git init && git add . && git commit -m "trading system"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
   (`.env`, `paper_state.db`, and the data cache are gitignored — your keys are
   never pushed.)

2. **Add your Alpaca PAPER keys as repo secrets.** In the repo:
   Settings → Secrets and variables → Actions → New repository secret. Add
   `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. These stay encrypted; only the
   workflows read them.

3. **Prime the start line** (begin flat at $1,000). In the repo:
   Actions → paper-trade → Run workflow → set `mode` = `prime`. This sets the
   cursors to now and trades nothing; everything from here is genuine forward
   testing.

4. **Let it run.** `paper-trade` then runs every ~15 min during US market hours
   and updates the ledger. `daily-briefing` runs at 02:00 UTC and writes
   `briefings/latest.md`.

5. **Add the Cowork briefing routine.** Create a scheduled Cowork routine at
   **09:00 Asia/Bangkok** whose task is: pull this repo and display
   `briefings/latest.md`. It needs no keys — it only reads a file the workflow
   already generated.

## Paper trading (broker mode) vs ledger-sim
- **Ledger-sim (default):** no orders are placed; fills are simulated in the
  SQLite ledger. Good for a dry run of the loop.
- **Broker mode (real paper trading):** set a repo **variable** `SUBMIT_BROKER`
  = `true` (Settings → Secrets and variables → Actions → Variables). Then the
  loop places **bracket orders** on your Alpaca paper account — a market entry
  plus a resting GTC stop at the 1% level, so the stop is enforced by the broker
  in real time (not by the 15-min polling). The Alpaca account becomes the
  source of truth, and the briefing reads it (`--broker`), normalising the
  account to a $1,000 starting display.

Account size: keep the Alpaca paper default (e.g. $100k) so whole-share bracket
orders work — results are identical in % at any size, and the dashboard shows
them normalised to $1,000. `--prime` records the account's baseline equity for
that normalisation.

## Notes
- Nothing touches real money. champion-plus is all equities.
- GitHub cron can drift 15-30 min under load — acceptable for entries on a
  bar-close paper test; the broker-side stop is unaffected by that drift.
- A tight-stop signal (e.g. 15m mean-reversion) can imply a large notional; if
  it exceeds buying power the bracket is rejected, logged, and skipped — the
  loop continues. That's a real constraint the paper test will surface.

## Checking in
- The latest briefing is always at `briefings/latest.md` in the repo.
- Run `python -m backtest.live.run --status` locally (with `.env`) to inspect
  the ledger any time you have the repo checked out.
