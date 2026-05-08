"""
options_executor.py — Alpaca options order execution.

Wraps alpaca-py TradingClient for options orders.
Detects options approval level on first call and caches to trader_config.json.

Key design decisions:
- Single-leg orders (long call/put, CSP, covered call) use LimitOrderRequest
  with the OCC contract symbol directly.
- Multi-leg orders (spreads, condors) use MultiLegOrderRequest (Level 3+).
- Mid-price limit orders only; no market orders on options (spreads too wide).
- All methods are synchronous and safe to call from a QThread.
"""
import logging
import time
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

_OPTIONS_LEVEL_CACHE: Optional[int] = None  # in-process cache


class OptionsExecutor:
    def __init__(self, trading_client):
        """trading_client is an already-authenticated alpaca TradingClient instance."""
        self._client = trading_client

    # ── Account / approval level ───────────────────────────────────────────────

    def detect_options_level(self) -> int:
        """
        Fetch options approval level from Alpaca account.
        Returns 0 if options not approved, 2 for Level 2, 3 for Level 3.
        Caches result to trader_config.json.
        """
        global _OPTIONS_LEVEL_CACHE
        if _OPTIONS_LEVEL_CACHE is not None:
            return _OPTIONS_LEVEL_CACHE

        try:
            account = self._client.get_account()
            level = int(getattr(account, "options_approved_level", 0) or 0)
        except Exception as exc:
            _log.warning("OptionsExecutor: could not fetch options level: %s", exc)
            level = 0

        _OPTIONS_LEVEL_CACHE = level
        self._persist_level(level)
        _log.info("OptionsExecutor: options approval level = %d", level)
        return level

    def _persist_level(self, level: int) -> None:
        """Save detected level to trader_config.json for reference."""
        try:
            from core.portfolio import load_trader_config, save_trader_config
            config = load_trader_config()
            if config.get("options_approval_level") != level:
                config["options_approval_level"] = level
                save_trader_config(config)
        except Exception as exc:
            _log.debug("OptionsExecutor: could not persist options level: %s", exc)

    # ── Options positions ──────────────────────────────────────────────────────

    def get_options_positions(self) -> List[Dict]:
        """
        Return list of open options positions as plain dicts.
        Each dict: {contract_symbol, option_type, strike, expiry, qty,
                    avg_entry_price, current_price, unrealized_pl, market_value}
        """
        try:
            all_positions = self._client.get_all_positions()
            result = []
            for p in all_positions:
                sym = str(p.symbol)
                # Options OCC symbols are longer than 6 chars and contain digits mid-string
                if len(sym) > 10 and any(c.isdigit() for c in sym[4:]):
                    result.append({
                        "contract_symbol": sym,
                        "qty":             float(p.qty),
                        "avg_entry_price": float(p.avg_entry_price),
                        "current_price":   float(p.current_price)   if p.current_price  else None,
                        "unrealized_pl":   float(p.unrealized_pl)   if p.unrealized_pl  else None,
                        "market_value":    float(p.market_value)    if p.market_value   else None,
                    })
            return result
        except Exception as exc:
            _log.warning("OptionsExecutor: get_options_positions failed: %s", exc)
            return []

    def get_option_current_price(self, contract_symbol: str) -> Optional[float]:
        """Fetch current mid-market price for an options contract."""
        try:
            positions = self.get_options_positions()
            for p in positions:
                if p["contract_symbol"] == contract_symbol:
                    return p.get("current_price")
        except Exception:
            pass
        return None

    # ── Single-leg orders ──────────────────────────────────────────────────────

    def buy_option(
        self,
        contract_symbol: str,
        contracts: int,
        limit_price: float,
    ) -> Tuple[Optional[str], str]:
        """
        Buy `contracts` option contracts at limit_price.
        Returns (order_id, status) or (None, error_message).
        """
        try:
            from alpaca.trading.requests import LimitOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            req = LimitOrderRequest(
                symbol=contract_symbol,
                qty=contracts,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
            )
            order = self._client.submit_order(req)
            oid = str(order.id)
            _log.info("OPTIONS BUY %s x%d @ $%.2f — order %s", contract_symbol, contracts, limit_price, oid)
            return oid, str(order.status)
        except Exception as exc:
            _log.error("OPTIONS BUY %s failed: %s", contract_symbol, exc)
            return None, str(exc)

    def sell_option(
        self,
        contract_symbol: str,
        contracts: int,
        limit_price: float,
    ) -> Tuple[Optional[str], str]:
        """
        Sell `contracts` contracts at limit_price (close long or open short).
        Returns (order_id, status) or (None, error_message).
        """
        try:
            from alpaca.trading.requests import LimitOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            req = LimitOrderRequest(
                symbol=contract_symbol,
                qty=contracts,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
            )
            order = self._client.submit_order(req)
            oid = str(order.id)
            _log.info("OPTIONS SELL %s x%d @ $%.2f — order %s", contract_symbol, contracts, limit_price, oid)
            return oid, str(order.status)
        except Exception as exc:
            _log.error("OPTIONS SELL %s failed: %s", contract_symbol, exc)
            return None, str(exc)

    def close_option_position(self, contract_symbol: str, contracts: int) -> Tuple[Optional[str], str]:
        """
        Close (flatten) an existing options position at market-ish price.
        Fetches current bid and places a limit slightly below to ensure fill.
        """
        try:
            self._client.close_position(contract_symbol)
            return "closed", "closed"
        except Exception:
            pass
        # Fallback: sell at current mid
        price = self.get_option_current_price(contract_symbol)
        if price and price > 0:
            limit = round(price * 0.99, 2)  # 1% below mid to prioritise fill
            return self.sell_option(contract_symbol, contracts, limit)
        return None, "could not determine current price for close"

    # ── Multi-leg orders (Level 3+) ────────────────────────────────────────────

    def submit_spread(
        self,
        legs: List[Dict],
        limit_price: float,
    ) -> Tuple[Optional[str], str]:
        """
        Submit a multi-leg spread order.
        legs: list of dicts with keys: contract_symbol, side ('buy'|'sell'), qty
        limit_price: net debit (positive) or net credit (negative) for the spread.

        Returns (order_id, status) or (None, error_message).
        """
        try:
            from alpaca.trading.requests import MultiLegOrderRequest, LegRequest
            from alpaca.trading.enums import OrderSide, TimeInForce

            leg_requests = []
            for leg in legs:
                side = OrderSide.BUY if leg["side"].lower() == "buy" else OrderSide.SELL
                leg_requests.append(LegRequest(
                    symbol=leg["contract_symbol"],
                    side=side,
                    qty=leg["qty"],
                ))

            req = MultiLegOrderRequest(
                legs=leg_requests,
                time_in_force=TimeInForce.DAY,
                limit_price=round(abs(limit_price), 2),
            )
            order = self._client.submit_order(req)
            oid = str(order.id)
            symbols = [l["contract_symbol"] for l in legs]
            _log.info("SPREAD ORDER %s @ $%.2f net — order %s", symbols, limit_price, oid)
            return oid, str(order.status)
        except Exception as exc:
            _log.error("SPREAD ORDER failed: %s", exc)
            return None, str(exc)

    # ── Fill polling ───────────────────────────────────────────────────────────

    def get_filled_price(self, order_id: str, max_wait_secs: int = 10) -> Optional[float]:
        """Poll for fill price. Returns filled_avg_price or None if not filled in time."""
        deadline = time.time() + max_wait_secs
        while time.time() < deadline:
            try:
                order = self._client.get_order_by_id(order_id)
                if order.filled_avg_price:
                    return float(order.filled_avg_price)
            except Exception:
                pass
            time.sleep(1)
        return None
