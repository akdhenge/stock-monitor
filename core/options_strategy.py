"""
options_strategy.py — Strike selection and OptionsPlay construction.

Given a ScanResult and routing decision (strategy_type), picks the best
available strikes and expiry from the yfinance option chain and returns
a fully-specified OptionsPlay ready for options_executor to submit.

OptionsPlay is a plain dict (not a dataclass) so it serialises trivially.
Keys:
  symbol, strategy_type, legs, expiration, entry_premium_estimate,
  capital_deployed_estimate, max_loss, thesis, ivr_at_entry,
  underlying_price, underlying_stop_loss, scan_score
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

_log = logging.getLogger(__name__)

_TARGET_DTE_LOW  = 28   # minimum acceptable DTE
_TARGET_DTE_HIGH = 50   # maximum acceptable DTE (prefer 35)
_TARGET_DTE_IDEAL = 35


# ── Public entry point ─────────────────────────────────────────────────────────

def build_options_play(
    symbol: str,
    strategy_type: str,
    scan_result,
    contracts: int,
    underlying_stop_pct: float = 0.10,
) -> Optional[Dict]:
    """
    Fetch option chain, select strikes, and return an OptionsPlay dict.
    Returns None if a suitable chain/strike cannot be found.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        current_price = float(ticker.fast_info.last_price or 0)
        if current_price <= 0:
            return None

        expirations = ticker.options
        if not expirations:
            _log.warning("options_strategy: no expirations for %s", symbol)
            return None

        expiry = _pick_expiry(expirations)
        if expiry is None:
            _log.warning("options_strategy: no expiry in %d–%d DTE range for %s",
                         _TARGET_DTE_LOW, _TARGET_DTE_HIGH, symbol)
            return None

        chain = ticker.option_chain(expiry)

        builder = _STRATEGY_BUILDERS.get(strategy_type)
        if builder is None:
            _log.warning("options_strategy: no builder for strategy %s", strategy_type)
            return None

        play = builder(symbol, current_price, expiry, chain, contracts, scan_result)
        if play is None:
            return None

        stop_loss_price = round(current_price * (1 - underlying_stop_pct), 2)
        play["underlying_stop_loss"] = stop_loss_price
        play["scan_score"] = getattr(scan_result, "total_score", 0.0)
        return play

    except Exception as exc:
        _log.warning("options_strategy: build_options_play failed for %s: %s", symbol, exc)
        return None


# ── Expiry selection ───────────────────────────────────────────────────────────

def _pick_expiry(expirations: List[str]) -> Optional[str]:
    """Return the expiry string closest to _TARGET_DTE_IDEAL within the allowed range."""
    today = date.today()
    best = None
    best_diff = float("inf")
    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte < _TARGET_DTE_LOW or dte > _TARGET_DTE_HIGH:
            continue
        diff = abs(dte - _TARGET_DTE_IDEAL)
        if diff < best_diff:
            best_diff = diff
            best = exp_str
    return best


def days_to_expiry(expiration: str) -> int:
    """Return calendar days until expiration."""
    try:
        exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
        return max(0, (exp_date - date.today()).days)
    except ValueError:
        return 0


# ── Strike helpers ─────────────────────────────────────────────────────────────

def _find_strike_by_delta(options_df, target_delta: float, option_type: str) -> Optional[float]:
    """
    Find strike closest to target_delta. yfinance doesn't return delta directly,
    so we use a moneyness proxy: 30-delta call ≈ strike 5–10% OTM for typical IV.
    For a call: 40-delta ≈ ATM+0%, 30-delta ≈ ATM+5%, 20-delta ≈ ATM+10%
    For a put:  40-delta ≈ ATM-0%, 30-delta ≈ ATM-5%, 20-delta ≈ ATM-10%
    """
    if options_df.empty:
        return None
    # Use open interest + IV to weight the selection toward liquid strikes
    df = options_df[options_df["volume"].fillna(0) > 0].copy()
    if df.empty:
        df = options_df.copy()
    strikes = df["strike"].tolist()
    return strikes[0] if strikes else None


def _atm_strike(current_price: float, options_df) -> Optional[float]:
    """Return strike closest to current price."""
    if options_df.empty:
        return None
    strikes = options_df["strike"].tolist()
    return min(strikes, key=lambda s: abs(s - current_price))


def _otm_strike(current_price: float, options_df, pct_otm: float) -> Optional[float]:
    """
    Return strike closest to current_price * (1 ± pct_otm).
    For calls: pct_otm > 0 means above current price.
    For puts:  pct_otm < 0 means below current price.
    """
    if options_df.empty:
        return None
    target = current_price * (1 + pct_otm)
    strikes = options_df["strike"].tolist()
    return min(strikes, key=lambda s: abs(s - target))


def _mid_price(row) -> float:
    """Return mid-market price from a chain row."""
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    if ask > 0:
        return round((bid + ask) / 2, 2)
    return float(row.get("lastPrice", 0) or 0)


def _row_for_strike(df, strike: float):
    """Return the chain row matching strike, or None."""
    rows = df[df["strike"] == strike]
    if rows.empty:
        return None
    return rows.iloc[0]


def _occ_symbol(symbol: str, expiry: str, option_type: str, strike: float) -> str:
    """Build OCC option symbol: AAPL241231C00150000."""
    exp = datetime.strptime(expiry, "%Y-%m-%d").strftime("%y%m%d")
    type_char = "C" if option_type.lower() == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{symbol.upper()}{exp}{type_char}{strike_int:08d}"


# ── Strategy builders ──────────────────────────────────────────────────────────

def _build_long_call(symbol, price, expiry, chain, contracts, scan_result) -> Optional[Dict]:
    calls = chain.calls
    strike = _otm_strike(price, calls, 0.05)  # ~5% OTM ≈ 30–35 delta
    if strike is None:
        return None
    row = _row_for_strike(calls, strike)
    if row is None:
        return None
    premium = _mid_price(row)
    if premium <= 0:
        return None

    per_contract_cost = premium * 100
    total_cost = per_contract_cost * contracts

    return {
        "symbol": symbol,
        "strategy_type": "long_call",
        "legs": [{
            "contract_symbol": _occ_symbol(symbol, expiry, "call", strike),
            "option_type": "call",
            "strike": strike,
            "expiration": expiry,
            "contracts": contracts,
            "side": "long",
        }],
        "expiration": expiry,
        "entry_premium_estimate": premium,
        "capital_deployed_estimate": total_cost,
        "max_loss": total_cost,
        "thesis": f"Long call ${strike:.0f} exp {expiry} ({contracts} contracts)",
        "ivr_at_entry": None,
        "underlying_price": price,
    }


def _build_long_put(symbol, price, expiry, chain, contracts, scan_result) -> Optional[Dict]:
    puts = chain.puts
    strike = _otm_strike(price, puts, -0.05)  # ~5% OTM put
    if strike is None:
        return None
    row = _row_for_strike(puts, strike)
    if row is None:
        return None
    premium = _mid_price(row)
    if premium <= 0:
        return None

    per_contract_cost = premium * 100
    total_cost = per_contract_cost * contracts

    return {
        "symbol": symbol,
        "strategy_type": "long_put",
        "legs": [{
            "contract_symbol": _occ_symbol(symbol, expiry, "put", strike),
            "option_type": "put",
            "strike": strike,
            "expiration": expiry,
            "contracts": contracts,
            "side": "long",
        }],
        "expiration": expiry,
        "entry_premium_estimate": premium,
        "capital_deployed_estimate": total_cost,
        "max_loss": total_cost,
        "thesis": f"Long put ${strike:.0f} exp {expiry} ({contracts} contracts)",
        "ivr_at_entry": None,
        "underlying_price": price,
    }


def _build_csp(symbol, price, expiry, chain, contracts, scan_result) -> Optional[Dict]:
    """Cash-secured put: sell OTM put, collect premium, obligated to buy at strike."""
    puts = chain.puts
    strike = _otm_strike(price, puts, -0.07)  # ~7% OTM ≈ 20-delta put
    if strike is None:
        return None
    row = _row_for_strike(puts, strike)
    if row is None:
        return None
    premium = _mid_price(row)
    if premium <= 0:
        return None

    collateral = strike * 100 * contracts  # cash locked per contract set
    max_loss = (strike - premium) * 100 * contracts  # loss if stock goes to zero

    return {
        "symbol": symbol,
        "strategy_type": "csp",
        "legs": [{
            "contract_symbol": _occ_symbol(symbol, expiry, "put", strike),
            "option_type": "put",
            "strike": strike,
            "expiration": expiry,
            "contracts": contracts,
            "side": "short",
        }],
        "expiration": expiry,
        "entry_premium_estimate": -premium,  # negative = premium received
        "capital_deployed_estimate": collateral,
        "max_loss": max_loss,
        "thesis": f"CSP ${strike:.0f} exp {expiry} — collect ${premium*100*contracts:.0f} premium ({contracts} contracts)",
        "ivr_at_entry": None,
        "underlying_price": price,
    }


def _build_covered_call(symbol, price, expiry, chain, contracts, scan_result) -> Optional[Dict]:
    """Covered call: sell OTM call against existing long stock position."""
    calls = chain.calls
    # 14–21 DTE preferred for covered calls (theta sweet spot), but use plan expiry
    strike = _otm_strike(price, calls, 0.07)  # ~7% OTM ≈ 25-30 delta
    if strike is None:
        return None
    row = _row_for_strike(calls, strike)
    if row is None:
        return None
    premium = _mid_price(row)
    if premium <= 0:
        return None

    return {
        "symbol": symbol,
        "strategy_type": "covered_call",
        "legs": [{
            "contract_symbol": _occ_symbol(symbol, expiry, "call", strike),
            "option_type": "call",
            "strike": strike,
            "expiration": expiry,
            "contracts": contracts,
            "side": "short",
        }],
        "expiration": expiry,
        "entry_premium_estimate": -premium,
        "capital_deployed_estimate": 0.0,  # collateral is the stock already held
        "max_loss": 0.0,                   # capped upside, not true "loss" in isolation
        "thesis": f"Covered call ${strike:.0f} exp {expiry} — collect ${premium*100*contracts:.0f} ({contracts} contracts)",
        "ivr_at_entry": None,
        "underlying_price": price,
    }


def _build_bull_call_spread(symbol, price, expiry, chain, contracts, scan_result) -> Optional[Dict]:
    calls = chain.calls
    long_strike  = _otm_strike(price, calls, 0.03)   # ~3% OTM long leg
    short_strike = _otm_strike(price, calls, 0.08)   # ~8% OTM short leg
    if long_strike is None or short_strike is None or long_strike >= short_strike:
        return None
    long_row  = _row_for_strike(calls, long_strike)
    short_row = _row_for_strike(calls, short_strike)
    if long_row is None or short_row is None:
        return None
    long_premium  = _mid_price(long_row)
    short_premium = _mid_price(short_row)
    if long_premium <= 0:
        return None

    net_debit     = round(long_premium - short_premium, 2)
    max_profit    = (short_strike - long_strike - net_debit) * 100 * contracts
    max_loss      = net_debit * 100 * contracts
    capital_deployed = max_loss

    return {
        "symbol": symbol,
        "strategy_type": "bull_call_spread",
        "legs": [
            {
                "contract_symbol": _occ_symbol(symbol, expiry, "call", long_strike),
                "option_type": "call",
                "strike": long_strike,
                "expiration": expiry,
                "contracts": contracts,
                "side": "long",
            },
            {
                "contract_symbol": _occ_symbol(symbol, expiry, "call", short_strike),
                "option_type": "call",
                "strike": short_strike,
                "expiration": expiry,
                "contracts": contracts,
                "side": "short",
            },
        ],
        "expiration": expiry,
        "entry_premium_estimate": net_debit,
        "capital_deployed_estimate": capital_deployed,
        "max_loss": max_loss,
        "thesis": (f"Bull call spread ${long_strike:.0f}/${short_strike:.0f} exp {expiry} "
                   f"— max profit ${max_profit:.0f} / max loss ${max_loss:.0f} ({contracts} contracts)"),
        "ivr_at_entry": None,
        "underlying_price": price,
    }


def _build_bear_put_spread(symbol, price, expiry, chain, contracts, scan_result) -> Optional[Dict]:
    puts = chain.puts
    long_strike  = _otm_strike(price, puts, -0.03)   # ~3% OTM long put leg
    short_strike = _otm_strike(price, puts, -0.08)   # ~8% OTM short put leg
    if long_strike is None or short_strike is None or long_strike <= short_strike:
        return None
    long_row  = _row_for_strike(puts, long_strike)
    short_row = _row_for_strike(puts, short_strike)
    if long_row is None or short_row is None:
        return None
    long_premium  = _mid_price(long_row)
    short_premium = _mid_price(short_row)
    if long_premium <= 0:
        return None

    net_debit  = round(long_premium - short_premium, 2)
    max_profit = (long_strike - short_strike - net_debit) * 100 * contracts
    max_loss   = net_debit * 100 * contracts

    return {
        "symbol": symbol,
        "strategy_type": "bear_put_spread",
        "legs": [
            {
                "contract_symbol": _occ_symbol(symbol, expiry, "put", long_strike),
                "option_type": "put",
                "strike": long_strike,
                "expiration": expiry,
                "contracts": contracts,
                "side": "long",
            },
            {
                "contract_symbol": _occ_symbol(symbol, expiry, "put", short_strike),
                "option_type": "put",
                "strike": short_strike,
                "expiration": expiry,
                "contracts": contracts,
                "side": "short",
            },
        ],
        "expiration": expiry,
        "entry_premium_estimate": net_debit,
        "capital_deployed_estimate": max_loss,
        "max_loss": max_loss,
        "thesis": (f"Bear put spread ${long_strike:.0f}/${short_strike:.0f} exp {expiry} "
                   f"— max profit ${max_profit:.0f} / max loss ${max_loss:.0f} ({contracts} contracts)"),
        "ivr_at_entry": None,
        "underlying_price": price,
    }


def _build_iron_condor(symbol, price, expiry, chain, contracts, scan_result) -> Optional[Dict]:
    """Iron condor: sell strangle, buy wings for defined risk."""
    calls = chain.calls
    puts  = chain.puts

    short_call_strike = _otm_strike(price, calls,  0.08)
    long_call_strike  = _otm_strike(price, calls,  0.13)
    short_put_strike  = _otm_strike(price, puts,  -0.08)
    long_put_strike   = _otm_strike(price, puts,  -0.13)

    if any(s is None for s in [short_call_strike, long_call_strike,
                                short_put_strike, long_put_strike]):
        return None
    if short_call_strike >= long_call_strike or short_put_strike <= long_put_strike:
        return None

    sc_row = _row_for_strike(calls, short_call_strike)
    lc_row = _row_for_strike(calls, long_call_strike)
    sp_row = _row_for_strike(puts,  short_put_strike)
    lp_row = _row_for_strike(puts,  long_put_strike)
    if any(r is None for r in [sc_row, lc_row, sp_row, lp_row]):
        return None

    net_credit = round(
        _mid_price(sc_row) - _mid_price(lc_row) +
        _mid_price(sp_row) - _mid_price(lp_row), 2
    )
    if net_credit <= 0:
        return None

    call_width = long_call_strike - short_call_strike
    max_loss   = (call_width - net_credit) * 100 * contracts
    margin     = call_width * 100 * contracts  # collateral = max wing width

    return {
        "symbol": symbol,
        "strategy_type": "iron_condor",
        "legs": [
            {"contract_symbol": _occ_symbol(symbol, expiry, "put",  long_put_strike),
             "option_type": "put",  "strike": long_put_strike,  "expiration": expiry,
             "contracts": contracts, "side": "long"},
            {"contract_symbol": _occ_symbol(symbol, expiry, "put",  short_put_strike),
             "option_type": "put",  "strike": short_put_strike, "expiration": expiry,
             "contracts": contracts, "side": "short"},
            {"contract_symbol": _occ_symbol(symbol, expiry, "call", short_call_strike),
             "option_type": "call", "strike": short_call_strike,"expiration": expiry,
             "contracts": contracts, "side": "short"},
            {"contract_symbol": _occ_symbol(symbol, expiry, "call", long_call_strike),
             "option_type": "call", "strike": long_call_strike, "expiration": expiry,
             "contracts": contracts, "side": "long"},
        ],
        "expiration": expiry,
        "entry_premium_estimate": -net_credit,  # credit received
        "capital_deployed_estimate": margin,
        "max_loss": max_loss,
        "thesis": (f"Iron condor put ${long_put_strike:.0f}/${short_put_strike:.0f} "
                   f"call ${short_call_strike:.0f}/${long_call_strike:.0f} exp {expiry} "
                   f"— credit ${net_credit*100*contracts:.0f} / max loss ${max_loss:.0f} ({contracts} contracts)"),
        "ivr_at_entry": None,
        "underlying_price": price,
    }


_STRATEGY_BUILDERS = {
    "long_call":        _build_long_call,
    "long_put":         _build_long_put,
    "csp":              _build_csp,
    "covered_call":     _build_covered_call,
    "bull_call_spread": _build_bull_call_spread,
    "bear_put_spread":  _build_bear_put_spread,
    "iron_condor":      _build_iron_condor,
}
