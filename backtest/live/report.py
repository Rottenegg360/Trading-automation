"""
Daily briefings for the Cowork dashboard (READ-ONLY — never trades).

Two reports, designed to be delivered by scheduled Cowork routines:
  * morning  : pre-day market context — recent moves, key levels, open
               positions + stops, and which strategies are armed by regime.
  * night    : end-of-day performance — equity, day P&L, per-strategy
               attribution, win/loss, open exposure, drawdown vs limit.

Source of truth is the SQLite ledger (the $1,000 forward test); live market
context comes from Alpaca read endpoints. Nothing here submits orders.

    python -m backtest.live.report --morning
    python -m backtest.live.report --night
"""
from __future__ import annotations

import argparse

import pandas as pd

from ..engine.indicators import adx, atr, rolling_z, sma
from ..run_backtest import CHAMPION_PLUS_SPECS
from .broker import PaperBroker
from .ledger import Ledger

ICT = "Asia/Bangkok"


def _pct(a: float, b: float) -> float:
    return 0.0 if b == 0 else round(100 * (a / b - 1), 2)


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def _broker_view(led: Ledger) -> dict | None:
    """In broker mode, read the Alpaca PAPER account (the source of truth) and
    normalise dollar figures to the $1,000 display baseline recorded at prime."""
    baseline = led.get_meta("account_baseline")
    if baseline is None:
        return None
    base = float(baseline)
    broker = PaperBroker()
    try:
        equity = broker.account_equity()
        positions = broker.get_positions()
    except Exception:
        return None
    norm = (1000.0 / base) if base else 1.0  # scale $100k account -> $1,000 view
    return {"base": base, "equity_real": equity, "norm": norm,
            "equity": equity * norm, "ret_pct": _pct(equity, base),
            "positions": positions, "tags": led.open_positions()}


def _market_context() -> dict:
    """Per-instrument recent move, levels, and regime read from live bars."""
    broker = PaperBroker()
    ctx = {}
    for sym, tf, mod in CHAMPION_PLUS_SPECS:
        try:
            df = broker.recent_bars(sym, tf)
        except Exception:
            continue
        if len(df) < 60:
            continue
        strat = mod.__name__.rsplit(".", 1)[-1]
        last = float(df["close"].iloc[-1])
        prior = float(df["close"].iloc[-7]) if len(df) > 7 else last
        a = float(atr(df, 14).iloc[-1])
        hi = float(df["high"].iloc[-20:].max())
        lo = float(df["low"].iloc[-20:].min())
        row = {"strat": strat, "tf": tf, "last": last, "move_pct": _pct(last, prior),
               "hi20": hi, "lo20": lo, "atr": a}
        if strat == "trend_following":
            ax = float(adx(df, 14).iloc[-1])
            fast, slow = float(sma(df["close"], 20).iloc[-1]), float(sma(df["close"], 50).iloc[-1])
            row["adx"] = round(ax, 1)
            row["dir"] = "up" if fast > slow else "down"
            row["armed"] = ax >= 20  # champion gate
        else:
            z = float(rolling_z(df["close"], 20).iloc[-1])
            row["z"] = round(z, 2)
            row["armed"] = abs(z) >= 2.0
        ctx[sym] = row
    return ctx


def _account_for_report(led: Ledger, ctx: dict, broker_mode: bool) -> dict:
    """Unified account snapshot for either mode. Broker mode reads the live
    Alpaca account (normalised to $1,000); sim mode reads the ledger."""
    last_prices = {s: c["last"] for s, c in ctx.items()}
    if broker_mode:
        bv = _broker_view(led)
        if bv is not None:
            tags, norm = bv["tags"], bv["norm"]
            positions = [{"sym": s, "direction": p["direction"], "entry": p["avg_entry"],
                          "stop": tags[s].stop if s in tags else None,
                          "upnl": p["unrealized_pl"] * norm,
                          "strategy": tags[s].strategy if s in tags else "?"}
                         for s, p in bv["positions"].items()]
            return {"equity": bv["equity"], "equity_raw": bv["equity_real"], "start": 1000.0,
                    "ret": bv["ret_pct"], "positions": positions,
                    "src": "Alpaca paper (normalised to $1,000)"}
    eq = led.equity(last_prices)
    positions = [{"sym": s, "direction": p.direction, "entry": p.entry, "stop": p.stop,
                  "upnl": (last_prices.get(s, p.entry) - p.entry) * p.size * p.direction,
                  "strategy": p.strategy} for s, p in led.open_positions().items()]
    return {"equity": eq, "equity_raw": eq, "start": led.starting_equity,
            "ret": _pct(eq, led.starting_equity), "positions": positions, "src": "ledger sim"}


def morning_report(ctx: dict | None = None, broker_mode: bool = False) -> str:
    led = Ledger()
    now_ict = pd.Timestamp.now(tz=ICT)
    ctx = _market_context() if ctx is None else ctx
    acct = _account_for_report(led, ctx, broker_mode)

    L = []
    L.append(f"MORNING BRIEFING - {now_ict:%a %d %b %Y, %H:%M} ICT")
    L.append(f"Account: {_fmt_money(acct['equity'])}  (start {_fmt_money(acct['start'])}, "
             f"{acct['ret']:+.2f}%)  [{acct['src']}]")

    L.append("\nRecent moves & levels:")
    for s, c in ctx.items():
        L.append(f"  {s:<4} {c['last']:>8.2f}  {c['move_pct']:+5.2f}%   "
                 f"range {c['lo20']:.2f} to {c['hi20']:.2f}  ATR {c['atr']:.2f}")

    L.append(f"\nOpen positions: {len(acct['positions'])}")
    for p in acct["positions"]:
        stop = f"{p['stop']:.2f}" if p["stop"] is not None else "n/a"
        L.append(f"  {p['sym']:<4} {'LONG' if p['direction']>0 else 'SHORT':<5} @ {p['entry']:.2f}  "
                 f"stop {stop}  uPnL {_fmt_money(p['upnl'])} ({p['strategy']})")

    armed_t = [f"{s}({ctx[s]['dir']}, ADX {ctx[s]['adx']})"
               for s, c in ctx.items() if c["strat"] == "trend_following" and c["armed"]]
    armed_m = [f"{s}(z {ctx[s]['z']})"
               for s, c in ctx.items() if c["strat"] == "mean_reversion" and c["armed"]]
    L.append("\nStrategies armed (by regime):")
    L.append(f"  trend-following : {', '.join(armed_t) or 'none - ADX below 20 everywhere'}")
    L.append(f"  mean-reversion  : {', '.join(armed_m) or 'none - no stretched z-score'}")
    return "\n".join(L)


def night_report(ctx: dict | None = None, broker_mode: bool = False) -> str:
    led = Ledger()
    ctx = _market_context() if ctx is None else ctx
    now_ict = pd.Timestamp.now(tz=ICT)
    acct = _account_for_report(led, ctx, broker_mode)
    closed = led.closed_trades()

    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=24)
    today = [t for t in closed if pd.Timestamp(t["exit_ts"]) >= cutoff]
    realized_today = sum(t["pnl"] for t in today)
    wins = [t for t in today if t["pnl"] > 0]

    peak = float(led.get_meta("peak_equity") or acct["equity_raw"])
    dd = _pct(acct["equity_raw"], peak)

    L = []
    L.append(f"END-OF-DAY PERFORMANCE - {now_ict:%a %d %b %Y, %H:%M} ICT")
    L.append(f"Equity: {_fmt_money(acct['equity'])}  ({acct['ret']:+.2f}% since start)  [{acct['src']}]")
    L.append(f"Realized P&L (last 24h): {_fmt_money(realized_today)}  "
             f"over {len(today)} trades ({len(wins)}W / {len(today)-len(wins)}L)")

    by = {}
    for t in today:
        by.setdefault(t["strategy"], 0.0)
        by[t["strategy"]] += t["pnl"]
    L.append("\nPer-strategy attribution (last 24h):")
    if by:
        for s, v in by.items():
            L.append(f"  {s:<18} {_fmt_money(v)}")
    else:
        L.append("  (no trades closed in the last 24h)")

    open_mtm = sum(p["upnl"] for p in acct["positions"])
    L.append(f"\nOpen exposure: {len(acct['positions'])} positions, unrealized {_fmt_money(open_mtm)}")
    for p in acct["positions"]:
        stop = f"{p['stop']:.2f}" if p["stop"] is not None else "n/a"
        L.append(f"  {p['sym']:<4} {'LONG' if p['direction']>0 else 'SHORT':<5} @ {p['entry']:.2f} stop {stop} ({p['strategy']})")

    L.append(f"\nDrawdown vs peak: {dd:.2f}%")
    L.append(f"Total closed trades since start: {len(closed)}")
    return "\n".join(L)


def daily_report(broker_mode: bool = False) -> str:
    """Combined 9am-ICT briefing: yesterday's session recap + today's setup,
    computing the live market context only once."""
    ctx = _market_context()
    bar = "=" * 60
    return (f"{bar}\nDAILY BRIEFING\n{bar}\n\n"
            "1) PERFORMANCE - completed US session\n"
            f"{night_report(ctx, broker_mode)}\n\n"
            f"{bar}\n\n2) SETUP - today's market context\n"
            f"{morning_report(ctx, broker_mode)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--morning", action="store_true")
    ap.add_argument("--night", action="store_true")
    ap.add_argument("--daily", action="store_true", help="combined recap + context (9am briefing)")
    ap.add_argument("--broker", action="store_true",
                    help="read the live Alpaca paper account (normalised to $1,000) instead of the sim ledger")
    args = ap.parse_args()
    if args.night:
        print(night_report(broker_mode=args.broker))
    elif args.morning:
        print(morning_report(broker_mode=args.broker))
    else:
        print(daily_report(broker_mode=args.broker))


if __name__ == "__main__":
    main()
