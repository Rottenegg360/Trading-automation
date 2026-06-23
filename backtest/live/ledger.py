"""
SQLite-backed paper ledger — the authoritative record of the forward test.

Tracks open positions, closed trades, and a per-symbol cursor (the last bar we
acted on) so the loop is restart-safe: kill the process, restart it, and it
picks up exactly where it left off without double-trading a bar.

Equity is derived, never stored stale:
    equity = starting_equity + realized_pnl + unrealized_mtm
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[2] / "paper_state.db"


@dataclass
class Position:
    symbol: str
    strategy: str
    direction: int
    entry_ts: str
    entry: float
    stop: float
    size: float


class Ledger:
    def __init__(self, db_path: str | Path = DEFAULT_DB, starting_equity: float = 1000.0):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        if self.get_meta("starting_equity") is None:
            self.set_meta("starting_equity", str(starting_equity))

    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS open_positions (
                symbol TEXT PRIMARY KEY, strategy TEXT, direction INTEGER,
                entry_ts TEXT, entry REAL, stop REAL, size REAL
            );
            CREATE TABLE IF NOT EXISTS closed_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, strategy TEXT, direction INTEGER,
                entry_ts TEXT, entry REAL, exit_ts TEXT, exit REAL,
                size REAL, pnl REAL, r_multiple REAL, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS cursor (symbol TEXT PRIMARY KEY, last_bar_ts TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self.conn.commit()

    # ---- meta ----
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str):
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    @property
    def starting_equity(self) -> float:
        return float(self.get_meta("starting_equity") or 1000.0)

    # ---- cursor ----
    def get_cursor(self, symbol: str) -> str | None:
        row = self.conn.execute("SELECT last_bar_ts FROM cursor WHERE symbol=?", (symbol,)).fetchone()
        return row["last_bar_ts"] if row else None

    def set_cursor(self, symbol: str, ts: str):
        self.conn.execute("INSERT INTO cursor(symbol,last_bar_ts) VALUES(?,?) "
                          "ON CONFLICT(symbol) DO UPDATE SET last_bar_ts=excluded.last_bar_ts", (symbol, ts))
        self.conn.commit()

    # ---- positions ----
    def open_positions(self) -> dict[str, Position]:
        rows = self.conn.execute("SELECT * FROM open_positions").fetchall()
        return {r["symbol"]: Position(r["symbol"], r["strategy"], r["direction"],
                                      r["entry_ts"], r["entry"], r["stop"], r["size"]) for r in rows}

    def add_position(self, p: Position):
        self.conn.execute(
            "INSERT OR REPLACE INTO open_positions VALUES (?,?,?,?,?,?,?)",
            (p.symbol, p.strategy, p.direction, p.entry_ts, p.entry, p.stop, p.size))
        self.conn.commit()

    def close_position(self, symbol: str, exit_ts: str, exit_px: float, reason: str,
                       fee_bps: float = 1.0) -> dict:
        p = self.open_positions().get(symbol)
        if p is None:
            return {}
        gross = (exit_px - p.entry) * p.size * p.direction
        fees = abs(p.entry * p.size) * fee_bps / 1e4 + abs(exit_px * p.size) * fee_bps / 1e4
        pnl = gross - fees
        init_risk = abs(p.entry - p.stop) * p.size
        r = pnl / init_risk if init_risk > 0 else 0.0
        self.conn.execute(
            "INSERT INTO closed_trades(symbol,strategy,direction,entry_ts,entry,exit_ts,exit,size,pnl,r_multiple,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (p.symbol, p.strategy, p.direction, p.entry_ts, p.entry, exit_ts, exit_px, p.size, pnl, r, reason))
        self.conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
        self.conn.commit()
        return {"symbol": symbol, "pnl": round(pnl, 2), "r": round(r, 3), "reason": reason}

    # ---- equity ----
    def realized_pnl(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(pnl),0) s FROM closed_trades").fetchone()
        return float(row["s"])

    def equity(self, last_prices: dict[str, float]) -> float:
        eq = self.starting_equity + self.realized_pnl()
        for p in self.open_positions().values():
            px = last_prices.get(p.symbol)
            if px is not None:
                eq += (px - p.entry) * p.size * p.direction
        return eq

    def closed_trades(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM closed_trades ORDER BY id").fetchall()]
