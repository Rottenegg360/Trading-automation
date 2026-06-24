"""
Forward-testing runner (paper). Runs the champion-plus config forward in real
time against a $1,000 ledger.

SAFE BY DEFAULT:
  * --dry-run is ON unless you pass --submit-broker (the ledger always records
    the forward test; broker submission to your Alpaca PAPER account is opt-in).
  * --once runs a single cycle (ideal for Windows Task Scheduler / cron every
    ~15 min). --loop runs continuously, sleeping between cycles.

Examples:
    python -m backtest.live.run --once                       # one cycle, ledger only
    python -m backtest.live.run --loop --interval 900        # every 15 min, ledger only
    python -m backtest.live.run --once --submit-broker       # also place PAPER orders
    python -m backtest.live.run --status                     # print ledger summary
"""
from __future__ import annotations

import argparse
import time

from .broker import PaperBroker
from .ledger import Ledger


def print_status(ledger: Ledger, broker: PaperBroker | None = None):
    closed = ledger.closed_trades()
    realized = ledger.realized_pnl()
    start = ledger.starting_equity
    wins = [t for t in closed if t["pnl"] > 0]
    print(f"\n=== PAPER FORWARD TEST — ledger ===")
    print(f"  starting equity : ${start:,.2f}")
    print(f"  realized P&L    : ${realized:,.2f}")
    print(f"  ledger equity*  : ${start + realized:,.2f}   (*excl. open MTM)")
    print(f"  closed trades   : {len(closed)}  (win {100*len(wins)/len(closed):.0f}%)" if closed
          else "  closed trades   : 0")
    pos = ledger.open_positions()
    print(f"  open positions  : {len(pos)}")
    for s, p in pos.items():
        print(f"      {s:<5} {'LONG' if p.direction>0 else 'SHORT'} size {p.size:.4f} @ {p.entry:.2f} stop {p.stop:.2f} ({p.strategy})")
    if broker is not None:
        try:
            print(f"  alpaca paper acct: ${broker.account_equity():,.2f} equity, {len(broker.trading.get_all_positions())} live positions")
        except Exception as e:
            print(f"  alpaca paper acct: (unavailable: {type(e).__name__})")


def _sleep_to_next_bar(period_min: int = 15, buffer_s: int = 3):
    """Sleep until ~buffer_s after the next 15-min boundary so a cycle fires
    right after each bar closes (not on a drifting fixed timer). 4h closes land
    on 15-min boundaries too, so this catches every instrument in the universe."""
    import pandas as pd
    now = pd.Timestamp.now(tz="UTC")
    nm = ((now.minute // period_min) + 1) * period_min
    nxt = now.floor("h") + pd.Timedelta(minutes=nm) + pd.Timedelta(seconds=buffer_s)
    time.sleep(max(1.0, (nxt - now).total_seconds()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--loop", action="store_true", help="run continuously")
    ap.add_argument("--interval", type=int, default=900, help="seconds between cycles in --loop (fixed mode)")
    ap.add_argument("--bar-aligned", action="store_true",
                    help="wake ~3s after each 15-min bar close instead of a fixed interval (recommended)")
    ap.add_argument("--equity", type=float, default=1000.0, help="starting ledger equity (first run only)")
    ap.add_argument("--corr-threshold", type=float, default=0.7)
    ap.add_argument("--submit-broker", action="store_true",
                    help="broker mode: place real BRACKET orders on Alpaca PAPER (account = source of truth)")
    ap.add_argument("--broker-test", action="store_true",
                    help="broker mode but DO NOT submit (reads account, logs intended bracket orders)")
    ap.add_argument("--status", action="store_true", help="print ledger status and exit")
    ap.add_argument("--prime", action="store_true",
                    help="set the starting line (cursors to now, no trades) for a clean forward test")
    args = ap.parse_args()

    ledger = Ledger(starting_equity=args.equity)
    broker = PaperBroker()

    if args.status:
        print_status(ledger, broker)
        return

    from .trader import prime, run_cycle

    if args.prime:
        n = prime(ledger, broker)
        print(f"primed {n} instruments. Forward test starts flat at ${ledger.starting_equity:,.0f}; "
              "run --once on a schedule (or --loop) to trade bars from here forward.")
        return

    broker_mode = args.submit_broker or args.broker_test
    submit = args.submit_broker

    def one():
        ev = run_cycle(ledger, broker, corr_threshold=args.corr_threshold,
                       broker_mode=broker_mode, submit=submit)
        mode = ("BROKER + PAPER ORDERS" if submit
                else "BROKER (no submit)" if broker_mode else "LEDGER-ONLY (dry-run)")
        print(f"[{ev['ts']}] {mode} | equity ${ev['equity']:,.2f} | "
              f"open {ev['open_positions']} | opened {len(ev['opened'])} closed {len(ev['closed'])}"
              + (f" blocked {len(ev.get('blocked', []))}" if ev.get('blocked') else ""))
        for o in ev["opened"]:
            print(f"    OPEN  {o['symbol']:<5} {'LONG' if o['dir']>0 else 'SHORT'} @ {o['entry']} stop {o['stop']} ({o['strategy']})")
        for c in ev["closed"]:
            if c:
                print(f"    CLOSE {c['symbol']:<5} pnl ${c['pnl']} ({c['r']}R, {c['reason']})")
        return ev

    if args.loop:
        kind = "bar-aligned (~3s after each 15-min close)" if args.bar_aligned else f"fixed {args.interval}s"
        print(f"forward-test loop started [{kind}, {'dry-run' if dry_run else 'PAPER ORDERS'}]. Ctrl-C to stop.")
        while True:
            if args.bar_aligned:
                _sleep_to_next_bar()
            try:
                one()
            except Exception as e:
                print(f"  cycle error: {type(e).__name__}: {e}")
            if not args.bar_aligned:
                time.sleep(args.interval)
    else:
        one()
        print_status(ledger)


if __name__ == "__main__":
    main()
