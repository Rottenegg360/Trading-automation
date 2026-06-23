"""Shared data contracts between layers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Signal:
    """Emitted by a strategy at a given bar. Stop is set BY THE STRATEGY in
    price terms (from ATR); the risk layer converts that to size so the
    dollar loss at stop == the account's per-trade risk budget."""
    ts: datetime
    symbol: str
    strategy: str
    direction: int          # +1 long, -1 short
    entry: float
    stop: float             # price level; distance to entry defines risk
    atr: float
    meta: dict = field(default_factory=dict)


@dataclass
class Trade:
    symbol: str
    strategy: str
    direction: int
    entry_ts: datetime
    entry: float
    stop: float
    size: float             # units of the instrument
    exit_ts: datetime | None = None
    exit: float | None = None
    exit_reason: str | None = None
    pnl: float = 0.0
    r_multiple: float = 0.0  # pnl in units of initial risk

    @property
    def is_open(self) -> bool:
        return self.exit is None
