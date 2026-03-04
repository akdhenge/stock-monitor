"""
AIResearcher — QThread that fetches recent news for a symbol via yfinance,
sends it to a local Ollama LLM or the Claude API, and emits structured results.

Signals:
  research_complete(dict) — keys: symbol, short_term, long_term, catalysts,
                                   sentiment, summary, timestamp, source
  research_error(str)     — human-readable error message
"""
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import yfinance as yf
from PyQt5.QtCore import QThread, pyqtSignal

from core.ai_research_store import get_cached_entry, save_entry
from core.scan_result import ScanResult


class AIResearcher(QThread):
    research_complete = pyqtSignal(dict)
    research_error    = pyqtSignal(str)

    def __init__(
        self,
        symbol: str,
        scan_result: Optional[ScanResult],
        settings: Dict[str, Any],
        force_refresh: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._symbol       = symbol.upper()
        self._scan_result  = scan_result
        self._settings     = settings
        self._force_refresh = force_refresh
        self._running      = False

    def stop(self) -> None:
        self._running = False

    # ── Entry point ────────────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True

        # 1. Check cache (unless force-refreshed)
        if not self._force_refresh:
            cached = get_cached_entry(self._symbol)
            if cached:
                self.research_complete.emit(cached)
                return

        # 2. Fetch news via yfinance
        try:
            ticker = yf.Ticker(self._symbol)
            raw_news = ticker.news or []
        except Exception as exc:
            self.research_error.emit(f"Failed to fetch news for {self._symbol}: {exc}")
            return

        # Filter to last 7 days, cap at 10 articles
        cutoff = datetime.now() - timedelta(days=7)
        articles = []
        for item in raw_news:
            try:
                pub_ts = item.get("providerPublishTime") or item.get("pubDate")
                if isinstance(pub_ts, (int, float)):
                    pub_dt = datetime.fromtimestamp(pub_ts)
                else:
                    pub_dt = datetime.now()
                if pub_dt >= cutoff:
                    articles.append({
                        "date":      pub_dt.strftime("%Y-%m-%d"),
                        "title":     item.get("title", ""),
                        "publisher": item.get("publisher", ""),
                    })
            except Exception:
                continue
            if len(articles) >= 10:
                break

        if not articles:
            self.research_error.emit(
                f"No news found for {self._symbol} in the last 7 days.\n"
                "Try again later or check the ticker symbol."
            )
            return

        if not self._running:
            return

        # 3. Build prompt
        prompt = self._build_prompt(articles)

        # 4. Call LLM
        provider = self._settings.get("ai_provider", "ollama")
        try:
            if provider == "claude":
                raw_response = self._call_claude(prompt)
            else:
                raw_response = self._call_ollama(prompt)
        except Exception as exc:
            self.research_error.emit(str(exc))
            return

        if not self._running:
            return

        # 5. Parse response
        result = self._parse_response(raw_response)
        result["symbol"]    = self._symbol
        result["timestamp"] = datetime.now().isoformat()
        result["source"]    = provider

        # 6. Cache and emit
        save_entry(self._symbol, result)
        self.research_complete.emit(result)

    # ── Prompt builder ─────────────────────────────────────────────────────────

    def _build_prompt(self, articles: list) -> str:
        news_lines = "\n".join(
            f"- [{a['date']}] {a['title']} ({a['publisher']})"
            for a in articles
        )

        fundamentals = ""
        r = self._scan_result
        if r is not None:
            parts = []
            if r.sector:
                parts.append(f"Sector: {r.sector}")
            if r.pe_ratio is not None:
                parts.append(f"P/E: {r.pe_ratio:.1f}")
            if r.rsi is not None:
                parts.append(f"RSI: {r.rsi:.0f}")
            parts.append(f"Total Score: {r.total_score:.1f}")
            if r.revenue_growth is not None:
                parts.append(f"Revenue Growth: {r.revenue_growth * 100:.1f}%")
            if r.roe is not None:
                parts.append(f"ROE: {r.roe * 100:.1f}%")
            fundamentals = "\n".join(f"- {p}" for p in parts)
        else:
            fundamentals = "- (No fundamentals available)"

        return (
            f"/no_think\n\n"
            f"You are a stock research analyst. Analyze recent news about {self._symbol} "
            f"and provide a concise investment outlook.\n\n"
            f"Recent news (last 7 days):\n{news_lines}\n\n"
            f"Stock fundamentals:\n{fundamentals}\n\n"
            f"Please respond in this EXACT format (one line per field):\n"
            f"SHORT_TERM: <1–2 sentence outlook for the next 1–4 weeks>\n"
            f"LONG_TERM: <1–2 sentence outlook for the next 6–18 months>\n"
            f"CATALYSTS: <comma-separated list of key near-term catalysts>\n"
            f"SENTIMENT: <exactly one of: BULLISH, BEARISH, NEUTRAL>\n"
            f"SUMMARY: <2–3 sentence overall summary>\n"
        )

    # ── LLM callers ───────────────────────────────────────────────────────────

    def _call_ollama(self, prompt: str) -> str:
        import urllib.request
        url   = self._settings.get("ai_ollama_url", "http://localhost:11434/api/generate")
        model = self._settings.get("ai_ollama_model", "mistral")
        payload = json.dumps({
            "model":  model,
            "prompt": prompt,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("response", "")
        except Exception as exc:
            raise RuntimeError(
                f"Ollama request failed: {exc}\n\n"
                "Make sure Ollama is running (https://ollama.com) and the model is pulled:\n"
                f"  ollama pull {model}"
            ) from exc

    def _call_claude(self, prompt: str) -> str:
        import urllib.request
        api_key = self._settings.get("ai_claude_api_key", "")
        model   = self._settings.get("ai_claude_model", "claude-haiku-20240307")
        if not api_key:
            raise RuntimeError(
                "Claude API key is not set.\n"
                "Go to Settings → AI and enter your Anthropic API key."
            )
        payload = json.dumps({
            "model": model,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type":    "application/json",
                "x-api-key":       api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                content = body.get("content", [])
                if content and isinstance(content, list):
                    return content[0].get("text", "")
                return ""
        except Exception as exc:
            raise RuntimeError(f"Claude API request failed: {exc}") from exc

    # ── Response parser ───────────────────────────────────────────────────────

    def _parse_response(self, text: str) -> Dict[str, str]:
        # Strip Qwen3 extended-thinking blocks before tag scan
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        result: Dict[str, str] = {
            "short_term": "",
            "long_term":  "",
            "catalysts":  "",
            "sentiment":  "NEUTRAL",
            "summary":    "",
        }
        tag_map = {
            "SHORT_TERM:": "short_term",
            "LONG_TERM:":  "long_term",
            "CATALYSTS:":  "catalysts",
            "SENTIMENT:":  "sentiment",
            "SUMMARY:":    "summary",
        }
        for line in text.splitlines():
            line = line.strip()
            for tag, key in tag_map.items():
                if line.upper().startswith(tag):
                    value = line[len(tag):].strip()
                    result[key] = value
                    break

        # Normalise sentiment
        sentiment = result["sentiment"].upper()
        if "BULL" in sentiment:
            result["sentiment"] = "BULLISH"
        elif "BEAR" in sentiment:
            result["sentiment"] = "BEARISH"
        else:
            result["sentiment"] = "NEUTRAL"

        # If parsing failed entirely, store the raw text in summary
        if not any(result[k] for k in ("short_term", "long_term", "summary")):
            result["summary"] = text.strip()

        return result
