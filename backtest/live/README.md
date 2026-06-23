# Paper forward testing

Runs the validated `champion-plus` config forward in real time against a
$1,000 ledger, reusing the exact backtest strategies + risk layer (1% stop,
vol sizing, correlation filter). The SQLite ledger (`paper_state.db`) is the
authoritative record and is restart-safe.

## Safety
- **Dry-run by default.** The ledger always records the forward test; orders
  are only sent to your Alpaca **paper** account if you add `--submit-broker`.
- Nothing touches real money. champion-plus is all equities (no crypto path).

## Start a clean forward test
```bash
python -m backtest.live.run --prime     # start flat at $1,000, cursors = now
```
This sets the starting line without trading. From here, only bars that close
*after* priming are traded — a genuine out-of-sample test.

## Run it forward
Pick one:

**A. Scheduled single cycles (recommended).** Run one cycle every ~15 min so
all timeframes (15m and 4h) are covered. On Windows, Task Scheduler:
```
Program:   <path>\python.exe
Arguments: -m backtest.live.run --once
Start in:  C:\Users\romeo\OneDrive\Desktop\Trading Automated
Trigger:   repeat every 15 minutes
```

**B. Persistent loop.**
```bash
python -m backtest.live.run --loop --interval 900
```

Add `--submit-broker` to either to also place orders on your Alpaca paper
account (off by default).

## Check progress
```bash
python -m backtest.live.run --status
```
Shows ledger equity, realized P&L, closed trades, and open positions (plus the
live Alpaca paper account balance for comparison).

## Notes
- A bar is only acted on once it is **confirmed closed** (`now >= bar_start +
  timeframe`), and only once (cursor guard) — safe to over-schedule.
- The $1,000 is tracked in the ledger regardless of your Alpaca account's
  actual paper balance, so sizing matches a $1,000 account.
