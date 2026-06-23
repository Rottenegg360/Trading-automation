"""
One forward-testing cycle for the champion-plus config.

Two modes:
  * ledger-sim (default, dry)   : simulates fills in the SQLite ledger; the loop
                                  polls the hard stop on each new bar.
  * broker     (--submit-broker): the Alpaca PAPER account is the source of
                                  truth. Entries are BRACKET orders (market +
                                  resting GTC stop), so the 1% stop is enforced
                                  by the broker in real time, not by polling.
                                  The ledger keeps strategy tags + cursors so
                                  briefings can attribute P&L per strategy.

Either way it acts only on CONFIRMED-CLOSED bars, once per bar (cursor), and
reuses the exact strategy + RiskManager logic from the backtest.
"""
from __future__ import annotations

import pandas as pd

from ..engine.data import TIMEFRAME_MINUTES
from ..engine.risk import RiskManager
from ..engine.types import Trade
from ..run_backtest import CHAMPION_PARAMS, CHAMPION_PLUS_SPECS, _accepted_kwargs
from .broker import PaperBroker
from .ledger import Ledger, Position

MIN_BARS = 80


def prime(ledger: Ledger, broker: PaperBroker, verbose: bool = True) -> int:
    """Set the forward-test starting line: cursors to the latest closed bar
    (no trades), and record the account's baseline equity for $-normalised
    reporting."""
    now = pd.Timestamp.now(tz="UTC")
    try:
        ledger.set_meta("account_baseline", str(broker.account_equity()))
    except Exception:
        pass
    primed = 0
    for sym, tf, _mod in CHAMPION_PLUS_SPECS:
        try:
            df = broker.recent_bars(sym, tf)
        except Exception:
            continue
        tf_delta = pd.Timedelta(minutes=TIMEFRAME_MINUTES[tf])
        closed = df.index[(df.index + tf_delta) <= now]
        if len(closed) == 0:
            continue
        ledger.set_cursor(sym, str(closed[-1]))
        primed += 1
        if verbose:
            print(f"  primed {sym:<5} cursor -> {closed[-1]}")
    return primed


def _returns_panel(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    cols = {s: df["close"].resample("1h").last().ffill().pct_change() for s, df in frames.items()}
    return pd.DataFrame(cols).dropna(how="all")


def _gather(broker: PaperBroker, verbose: bool):
    """Fetch recent bars and each symbol's latest CONFIRMED-CLOSED bar."""
    now = pd.Timestamp.now(tz="UTC")
    frames, meta = {}, {}
    for sym, tf, mod in CHAMPION_PLUS_SPECS:
        try:
            df = broker.recent_bars(sym, tf)
        except Exception as e:
            if verbose:
                print(f"  {sym}: fetch error ({type(e).__name__}: {str(e)[:50]})")
            continue
        if len(df) < MIN_BARS:
            continue
        tf_delta = pd.Timedelta(minutes=TIMEFRAME_MINUTES[tf])
        closed = df.index[(df.index + tf_delta) <= now]
        if len(closed) == 0:
            continue
        last_ts = closed[-1]
        i = df.index.get_loc(last_ts)
        frames[sym] = df
        meta[sym] = {"tf": tf, "mod": mod, "i": i, "last_ts": last_ts,
                     "price": float(df["close"].iloc[i])}
    return frames, meta, now


def run_cycle(ledger: Ledger, broker: PaperBroker, *, corr_threshold: float = 0.7,
              broker_mode: bool = False, submit: bool = False, verbose: bool = True) -> dict:
    risk = RiskManager(corr_threshold=corr_threshold)
    frames, meta, now = _gather(broker, verbose)
    last_prices = {s: m["price"] for s, m in meta.items()}
    panel = _returns_panel(frames)
    ev = {"ts": str(now), "opened": [], "closed": [], "blocked": []}

    # Tags = what we believe we own + which strategy owns it (ledger).
    tags = ledger.open_positions()
    # Source of truth for what is actually open.
    live = broker.get_positions() if broker_mode else {s: None for s in tags}

    # Reconcile (broker mode): a tagged symbol no longer at the broker means the
    # resting stop fired (or it was closed) — record the close for attribution.
    if broker_mode:
        for sym, p in list(tags.items()):
            if sym not in live:
                ev["closed"].append(ledger.close_position(sym, str(now), p.stop, "stop"))

    def new_bar(sym):
        m = meta.get(sym)
        if not m:
            return False
        cur = ledger.get_cursor(sym)
        return cur is None or pd.Timestamp(cur) < m["last_ts"]

    # 1) Manage exits on open positions (strategy exits; hard stop is broker-side
    #    in broker mode, polled in sim mode).
    for sym, p in list(ledger.open_positions().items()):
        if not new_bar(sym) or (broker_mode and sym not in live):
            continue
        m = meta[sym]
        df, i, bar = frames[sym], m["i"], frames[sym].iloc[m["i"]]
        if not broker_mode:
            if p.direction == 1 and bar["low"] <= p.stop:
                ev["closed"].append(ledger.close_position(sym, str(m["last_ts"]), p.stop, "stop"))
                continue
            if p.direction == -1 and bar["high"] >= p.stop:
                ev["closed"].append(ledger.close_position(sym, str(m["last_ts"]), p.stop, "stop"))
                continue
        tr = Trade(sym, p.strategy, p.direction, pd.Timestamp(p.entry_ts), p.entry, p.stop, p.size)
        reason = m["mod"].should_exit(tr, bar, df, i,
                                      **_accepted_kwargs(m["mod"].should_exit, CHAMPION_PARAMS.get(p.strategy, {})))
        if reason:
            if broker_mode and submit:
                try:
                    broker.close_symbol(sym)
                except Exception as e:
                    if verbose:
                        print(f"    close FAILED {sym}: {type(e).__name__}: {str(e)[:50]}")
            ev["closed"].append(ledger.close_position(sym, str(m["last_ts"]), m["price"], reason))

    # 2) Entries.
    equity = broker.account_equity() if broker_mode else ledger.equity(last_prices)
    held = set(broker.get_positions().keys()) if broker_mode else set(ledger.open_positions().keys())
    for sym, m in meta.items():
        if not new_bar(sym) or sym in held or sym in ledger.open_positions():
            continue
        mod, df, i = m["mod"], frames[sym], m["i"]
        strat = mod.__name__.rsplit(".", 1)[-1]
        sigs = mod.generate_signals(df, sym, **_accepted_kwargs(mod.generate_signals, CHAMPION_PARAMS.get(strat, {})))
        sig = next((s for s in sigs if s.ts == m["last_ts"]), None)
        if sig is None:
            continue
        open_syms = list(held | set(ledger.open_positions().keys()))
        ok, corr = risk.correlation_ok(sig, open_syms, panel.loc[:m["last_ts"]] if len(panel) else panel)
        if not ok:
            ev["blocked"].append({"symbol": sym, "max_corr": round(float(corr), 3)})
            continue
        raw = risk.size_for(sig, equity)
        size = int(round(raw)) if broker_mode else raw  # whole shares for bracket orders
        if size <= 0:
            continue
        if broker_mode and submit:
            try:
                oid = broker.submit_bracket(sym, size, sig.direction, sig.stop)
                if verbose:
                    print(f"    bracket {sym} {'BUY' if sig.direction>0 else 'SELL'} {size} stop {sig.stop:.2f} (id {oid})")
            except Exception as e:
                if verbose:
                    print(f"    bracket FAILED {sym}: {type(e).__name__}: {str(e)[:60]}")
                continue
        elif broker_mode and verbose:
            print(f"    [would bracket] {sym} {'BUY' if sig.direction>0 else 'SELL'} {size} @ ~{sig.entry:.2f} stop {sig.stop:.2f}")
        ledger.add_position(Position(sym, sig.strategy, sig.direction, str(sig.ts), sig.entry, sig.stop, float(size)))
        held.add(sym)
        ev["opened"].append({"symbol": sym, "dir": sig.direction, "entry": round(sig.entry, 2),
                             "stop": round(sig.stop, 2), "size": size, "strategy": sig.strategy})

    for sym, m in meta.items():
        if new_bar(sym):
            ledger.set_cursor(sym, str(m["last_ts"]))

    eq_now = equity if broker_mode else ledger.equity(last_prices)
    peak = float(ledger.get_meta("peak_equity") or eq_now)
    if eq_now > peak:
        ledger.set_meta("peak_equity", str(eq_now))
    ev["equity"] = round(eq_now, 2)
    ev["open_positions"] = len(held if broker_mode else ledger.open_positions())
    return ev
