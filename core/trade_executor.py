"""
trade_executor.py — Alpaca paper-trade execution.

Wraps alpaca-py TradingClient. All methods are synchronous and safe to call
from a QThread. Keys are read from settings (stored in data/settings.json
under 'alpaca_api_key' / 'alpaca_secret_key').

Slippage is modelled only in the fill recording — Alpaca applies its own
simulated fill logic for paper orders, which is already realistic.
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from core.market_clock import is_rth

_log = logging.getLogger(__name__)


class TradeExecutor:
    def __init__(self, settings: dict):
        api_key    = settings.get("alpaca_api_key", "").strip()
        secret_key = settings.get("alpaca_secret_key", "").strip()

        if not api_key or not secret_key:
            raise RuntimeError(
                "Alpaca API keys not configured.\n"
                "Go to Settings → Trader and enter your Alpaca Paper API key + secret."
            )

        from alpaca.trading.client import TradingClient
        self._client = TradingClient(api_key, secret_key, paper=True)
        _log.info("TradeExecutor: connected to Alpaca paper account")

    # ── Account ────────────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        """Return {equity, cash, buying_power, last_equity, day_pnl}."""
        acct = self._client.get_account()
        equity      = float(acct.equity)
        last_equity = float(acct.last_equity)
        return {
            "equity":       equity,
            "cash":         float(acct.cash),
            "buying_power": float(acct.buying_power),
            "last_equity":  last_equity,
            "day_pnl":      round(equity - last_equity, 2),
        }

    def get_positions(self) -> List[dict]:
        """Return list of open positions as plain dicts."""
        positions = self._client.get_all_positions()
        out = []
        for p in positions:
            out.append({
                "symbol":          p.symbol,
                "qty":             float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price":   float(p.current_price) if p.current_price else None,
                "market_value":    float(p.market_value)  if p.market_value  else None,
                "unrealized_pl":   float(p.unrealized_pl) if p.unrealized_pl else None,
                "cost_basis":      float(p.cost_basis)    if p.cost_basis    else None,
            })
        return out

    def get_all_time_high_nav(self) -> float:
        """Estimate ATH NAV from portfolio history (1Y lookback, daily)."""
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest
            req = GetPortfolioHistoryRequest(period="1A", timeframe="1D")
            hist = self._client.get_portfolio_history(history_filter=req)
            equities = [float(e) for e in (hist.equity or []) if e is not None]
            return max(equities) if equities else float(self._client.get_account().equity)
        except Exception:
            return float(self._client.get_account().equity)

    def get_spy_price(self) -> Optional[float]:
        """Fetch SPY last price for benchmark tracking."""
        try:
            pos_list = self._client.get_all_positions()
            # Use yfinance as fallback — same dep already in requirements
            import yfinance as yf
            price = yf.Ticker("SPY").fast_info.last_price
            return float(price) if price else None
        except Exception:
            return None

    # ── Order execution ────────────────────────────────────────────────────────

    def _get_ask_price(self, symbol: str) -> Optional[float]:
        """Fetch current ask price via yfinance. Returns None on failure."""
        try:
            import yfinance as yf
            info = yf.Ticker(symbol).fast_info
            ask = getattr(info, "ask", None) or getattr(info, "last_price", None)
            if ask and float(ask) > 0:
                return float(ask)
        except Exception:
            pass
        return None

    def buy(self, symbol: str, dollars: float) -> Tuple[Optional[str], str]:
        """
        Submit a limit buy order for `dollars` notional.
        Fetches current ask and places limit at ask + 0.1% to avoid chasing;
        falls back to market order if quote is unavailable.
        Returns (order_id, status) or (None, error_message).
        Only submits during RTH.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce

        if not is_rth():
            return None, "outside RTH — order not placed (queue manually or extend hours)"

        ask = self._get_ask_price(symbol)
        try:
            if ask:
                from alpaca.trading.requests import LimitOrderRequest
                limit_price = round(ask * 1.001, 2)  # 0.1% above ask ensures fill
                qty = max(1, int(dollars / limit_price))
                req = LimitOrderRequest(
                    symbol=symbol.upper(),
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                )
                _log.info("LIMIT BUY %s %d shares @ $%.2f (budget $%.0f)",
                          symbol, qty, limit_price, dollars)
            else:
                from alpaca.trading.requests import MarketOrderRequest
                req = MarketOrderRequest(
                    symbol=symbol.upper(),
                    notional=round(dollars, 2),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                _log.info("MARKET BUY %s $%.2f (no quote available)", symbol, dollars)

            order = self._client.submit_order(req)
            order_id = str(order.id)
            _log.info("BUY %s submitted — order %s status=%s", symbol, order_id, order.status)
            return order_id, str(order.status)
        except Exception as exc:
            _log.error("BUY %s failed: %s", symbol, exc)
            return None, str(exc)

    def sell(self, symbol: str) -> Tuple[Optional[str], str]:
        """
        Close the entire position in symbol at market.
        Returns (order_id, status) or (None, error_message).
        """
        try:
            order = self._client.close_position(symbol.upper())
            order_id = str(order.id)
            _log.info("SELL %s (close) — order %s status=%s", symbol, order_id, order.status)
            return order_id, str(order.status)
        except Exception as exc:
            _log.error("SELL %s failed: %s", symbol, exc)
            return None, str(exc)

    def get_filled_price(self, order_id: str, max_wait_secs: int = 10) -> Optional[float]:
        """
        Poll for fill price after submitting an order.
        Returns filled_avg_price or None if not yet filled within max_wait_secs.
        """
        import time
        deadline = time.time() + max_wait_secs
        while time.time() < deadline:
            try:
                import uuid
                from alpaca.trading.requests import GetOrderByIdRequest
                order = self._client.get_order_by_id(order_id)
                if order.filled_avg_price:
                    return float(order.filled_avg_price)
            except Exception:
                pass
            time.sleep(1)
        return None

    def get_filled_qty(self, order_id: str) -> Optional[float]:
        """Return filled_qty for a given order, or None on error."""
        try:
            order = self._client.get_order_by_id(order_id)
            return float(order.filled_qty) if order.filled_qty else None
        except Exception:
            return None
