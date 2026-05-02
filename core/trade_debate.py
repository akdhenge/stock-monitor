"""
trade_debate.py — Bull/Bear debate via Claude API before order execution.

run_debate() is called in trader_agent._process_scan() for each candidate
that has already passed hard gates and the decision score threshold.  It asks
Claude (Sonnet by default, configurable to Opus) to argue both sides and issue
a BUY or PASS verdict, with full awareness of the current market regime.

Design rules:
- Regime-aware: bear-market prompt explicitly frames the setup as a
  mean-reversion trade and asks whether the oversold thesis is sound.
  Bull-market prompt looks for momentum continuation quality.
- Fail-safe: Claude API errors return (True, "debate unavailable") so trading
  is never blocked by a temporary API failure.
- Model is configurable via trader_config.json key "debate_model".
  Default: claude-sonnet-4-6.  Set to claude-opus-4-7 for higher conviction.
"""
import json
import logging
import os
import re
import urllib.request
import urllib.error
from datetime import datetime
from typing import Dict, Optional, Tuple

_log = logging.getLogger(__name__)

_CLAUDE_URL     = "https://api.anthropic.com/v1/messages"
_DEFAULT_MODEL  = "claude-sonnet-4-6"
_TIMEOUT        = 90

_COST_PER_M: Dict[str, Dict[str, float]] = {
    "claude-sonnet-4-6":  {"input": 3.00,  "output": 15.00},
    "claude-opus-4-7":    {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}
_DEFAULT_COST = {"input": 3.00, "output": 15.00}
_DATA_DIR     = os.path.join(os.path.dirname(__file__), "..", "data")
_USAGE_LOG    = os.path.join(_DATA_DIR, "claude_usage_log.json")


def run_debate(
    symbol: str,
    scan_result,                    # ScanResult
    ai_research: Optional[dict],
    claude_rationale: str,
    decision_score: float,
    regime: str = "neutral",        # "bull" | "neutral" | "bear"
    settings: Optional[Dict] = None,
) -> Tuple[bool, str]:
    """
    Returns (proceed, verdict_summary).
      proceed=True  → trade confirmed, continue to execution.
      proceed=False → skip this symbol this scan cycle only.

    On Claude API failure returns (True, "debate unavailable") so a transient
    API error never silently blocks all trading for the session.
    """
    settings = settings or {}
    api_key  = settings.get("ai_claude_api_key", "").strip()
    model    = settings.get("debate_model", _DEFAULT_MODEL)

    if not api_key:
        _log.warning("trade_debate: no Claude API key — debate skipped for %s", symbol)
        return True, "debate skipped — no API key"

    prompt = _build_prompt(symbol, scan_result, ai_research, claude_rationale, decision_score, regime)

    try:
        payload = json.dumps({
            "model":      model,
            "max_tokens": 300,
            "messages":   [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            _CLAUDE_URL,
            data=payload,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        content = body.get("content", [])
        raw     = content[0].get("text", "").strip() if content else ""
        usage   = body.get("usage", {})
        in_tok  = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        _log_usage(model, symbol, regime, in_tok, out_tok)

        verdict, reason = _parse_verdict(raw)
        _log.info("debate [%s] %s → %s (model=%s in=%d out=%d): %s",
                  regime.upper(), symbol, verdict, model, in_tok, out_tok, reason[:100])
        return (verdict == "BUY"), reason

    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:200]
        _log.warning("trade_debate: Claude API %s for %s — %s — proceeding", exc, symbol, err_body)
        return True, f"debate unavailable ({exc}) — proceeding"
    except Exception as exc:
        _log.warning("trade_debate: error for %s (%s) — proceeding", symbol, exc)
        return True, "debate unavailable — proceeding"


# ── Regime-aware prompt ────────────────────────────────────────────────────────

_REGIME_CONTEXT = {
    "bull": (
        "The broad market is in a BULL regime (SPY above 200d MA, VIX < 20). "
        "Prioritize setups with technical momentum and trend confirmation. "
        "Be skeptical of stocks that are lagging the market or lack catalysts."
    ),
    "neutral": (
        "The broad market is in a NEUTRAL/TRANSITIONAL regime. "
        "Both mean-reversion and momentum setups can work. "
        "Weight the quality of the entry thesis and risk/reward carefully."
    ),
    "bear": (
        "The broad market is in a BEAR regime (SPY below 200d MA and/or elevated VIX). "
        "This is a mean-reversion / oversold-bounce context — NOT a momentum market. "
        "Technical scan scores are structurally low because nothing is in a technical uptrend; "
        "that is expected and not a reason to reject. Focus instead on: "
        "is the fundamental thesis intact? Is the stock genuinely oversold relative to its history? "
        "Is there a credible near-term catalyst? What would make this bounce fail?"
    ),
}

_REGIME_VERDICT_GUIDANCE = {
    "bull": (
        "BUY if the stock shows momentum, trend alignment, and the rationale is compelling. "
        "PASS if it's a laggard or the setup lacks technical confirmation."
    ),
    "neutral": (
        "BUY if the risk/reward is asymmetric and the thesis is specific. "
        "PASS if the setup is ambiguous or the stop is hard to define."
    ),
    "bear": (
        "BUY if this is a genuine oversold bounce with a clear catalyst or support level, "
        "and the downside is well-defined by the stop. "
        "PASS if the stock could continue lower with no floor in sight, or if the "
        "bear case is macro-driven and unlikely to resolve in the holding window."
    ),
}


def _build_prompt(
    symbol: str,
    scan_result,
    ai_research: Optional[dict],
    claude_rationale: str,
    decision_score: float,
    regime: str,
) -> str:
    rsi       = f"{scan_result.rsi:.0f}"    if scan_result.rsi   else "n/a"
    macd      = "bullish"                   if scan_result.macd_bullish else "not bullish"
    price     = f"${scan_result.price:.2f}" if scan_result.price  else "n/a"
    sector    = scan_result.sector          or "n/a"
    vol_spike = "yes" if getattr(scan_result, "volume_spike", False) else "no"
    week52h   = f"${scan_result.week52_high:.2f}" if getattr(scan_result, "week52_high", None) else "n/a"
    pct_off_high = ""
    if scan_result.price and getattr(scan_result, "week52_high", None):
        off = (scan_result.week52_high - scan_result.price) / scan_result.week52_high * 100
        pct_off_high = f" ({off:.0f}% off 52w high)"

    sentiment  = ""
    short_term = ""
    catalysts  = ""
    if ai_research:
        sentiment  = ai_research.get("sentiment", "")
        short_term = (ai_research.get("short_term") or "")[:150]
        catalysts  = (ai_research.get("catalysts")  or "")[:120]

    regime_ctx     = _REGIME_CONTEXT.get(regime, _REGIME_CONTEXT["neutral"])
    verdict_guide  = _REGIME_VERDICT_GUIDANCE.get(regime, _REGIME_VERDICT_GUIDANCE["neutral"])

    return (
        f"You are a senior portfolio risk manager evaluating a trade proposal.\n\n"
        f"MARKET CONTEXT: {regime_ctx}\n\n"
        f"TRADE PROPOSAL: {symbol} @ {price}{pct_off_high}\n"
        f"Sector: {sector} | 52w high: {week52h}\n"
        f"Scan score: {scan_result.total_score:.0f}/100 | Decision score: {decision_score:.0f}/100\n"
        f"RSI: {rsi} | MACD: {macd} | Volume spike: {vol_spike}\n"
        f"AI sentiment: {sentiment or 'not available'}\n"
        f"AI short-term view: {short_term or 'not available'}\n"
        f"AI catalysts: {catalysts or 'not available'}\n"
        f"Portfolio analyst rationale: {claude_rationale[:250]}\n\n"
        f"EVALUATION REQUIRED:\n"
        f"1. BULL CASE (1 sentence): strongest reason this trade works.\n"
        f"2. BEAR CASE (1 sentence): the main risk that kills this trade.\n"
        f"3. VERDICT GUIDANCE: {verdict_guide}\n\n"
        f"Respond with ONLY a JSON object, no prose before or after:\n"
        f'{{"verdict": "BUY", "bull": "...", "bear": "...", "reason": "..."}}'
    )


# ── Response parser ─────────────────────────────────────────────────────────────

def _parse_verdict(raw: str) -> Tuple[str, str]:
    # Strip markdown fences if present
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()

    # Primary: JSON with "verdict" key
    m = re.search(r'\{[^{}]*"verdict"[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            data    = json.loads(m.group())
            verdict = data.get("verdict", "BUY").upper().strip()
            reason  = str(data.get("reason", data.get("bull", text[:120])))
            return ("BUY" if verdict == "BUY" else "PASS"), reason
        except json.JSONDecodeError:
            pass

    # Fallback: plain VERDICT keyword
    vm = re.search(r"VERDICT[:\s]*(BUY|PASS)", text, re.IGNORECASE)
    if vm:
        return vm.group(1).upper(), text[:200]

    # Default to BUY on parse failure — don't block a trade on bad JSON
    return "BUY", f"parse fallback: {text[:80]}"


# ── Cost logging ────────────────────────────────────────────────────────────────

def _log_usage(model: str, symbol: str, regime: str, in_tok: int, out_tok: int) -> None:
    rates    = _COST_PER_M.get(model, _DEFAULT_COST)
    cost_usd = (in_tok / 1_000_000) * rates["input"] + (out_tok / 1_000_000) * rates["output"]
    record   = {
        "ts":            datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model":         model,
        "input_tokens":  in_tok,
        "output_tokens": out_tok,
        "cost_usd":      round(cost_usd, 6),
        "trigger":       f"debate:{symbol}:{regime}",
    }
    os.makedirs(_DATA_DIR, exist_ok=True)
    log: list = []
    if os.path.exists(_USAGE_LOG):
        try:
            with open(_USAGE_LOG, "r", encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            log = []
    log.append(record)
    try:
        with open(_USAGE_LOG, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        _log.debug("trade_debate: usage log write failed: %s", exc)
