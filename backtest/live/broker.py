"""
Alpaca PAPER broker wrapper.

Read paths (account, recent bars) are always safe. Order submission is OFF by
default and only used when the loop is run with --submit-broker; the SQLite
ledger is the authoritative forward-test record either way. champion-plus is
all equities, so there is no crypto order path here.
"""
from __future__ import annotations

import pandas as pd

from ..engine.data import TIMEFRAME_MINUTES, _load_env_keys, load_alpaca_bars

# Generous calendar windows so we always get enough CLOSED bars for indicators
# (champion uses slow MA 50, ADX 14, donchian 20 — a few hundred bars suffice).
_FETCH_DAYS = {"15m": 60, "1h": 150, "4h": 540}


class PaperBroker:
    def __init__(self):
        self.key, self.secret = _load_env_keys()
        self._tc = None

    @property
    def trading(self):
        if self._tc is None:
            from alpaca.trading.client import TradingClient
            self._tc = TradingClient(self.key, self.secret, paper=True)
        return self._tc

    def account_equity(self) -> float:
        return float(self.trading.get_account().equity)

    def get_positions(self) -> dict:
        """Open positions from the Alpaca PAPER account (the source of truth)."""
        out = {}
        for p in self.trading.get_all_positions():
            qty = float(p.qty)
            out[p.symbol] = {
                "qty": qty, "direction": 1 if qty > 0 else -1,
                "avg_entry": float(p.avg_entry_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
            }
        return out

    def submit_bracket(self, symbol: str, qty: int, direction: int, stop_price: float):
        """Market entry + a resting GTC stop at `stop_price` (OTO). The broker
        then enforces the 1% stop in real time, independent of the loop's
        polling cadence. Whole-share qty only (bracket/OTO requirement)."""
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest
        side = OrderSide.BUY if direction > 0 else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol, qty=int(abs(qty)), side=side,
            time_in_force=TimeInForce.GTC, order_class=OrderClass.OTO,
            stop_loss=StopLossRequest(stop_price=round(float(stop_price), 2)),
        )
        return self.trading.submit_order(req).id

    def close_symbol(self, symbol: str):
        """Cancel any resting orders for the symbol, then liquidate it."""
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            orders = self.trading.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
            for o in orders:
                self.trading.cancel_order_by_id(o.id)
        except Exception:
            pass
        return self.trading.close_position(symbol)

    def recent_bars(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Fresh (uncached) recent bars in the documented OHLCV contract."""
        days = _FETCH_DAYS.get(timeframe, 120)
        start = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=days)
        return load_alpaca_bars(symbol, timeframe, start=start, use_cache=False, verbose=False)

    def submit_market(self, symbol: str, qty: float, direction: int):
        """Best-effort market order to the PAPER account. Fractional qty via
        notional is not used here; qty is rounded to 4dp. Returns the order id
        or raises — the caller logs and continues (ledger is source of truth)."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        side = OrderSide.BUY if direction > 0 else OrderSide.SELL
        req = MarketOrderRequest(symbol=symbol, qty=round(abs(qty), 4),
                                 side=side, time_in_force=TimeInForce.DAY)
        return self.trading.submit_order(req).id
