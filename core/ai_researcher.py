"""
AIResearcher — QThread that fetches recent news for a symbol via yfinance,
sends it to a local Ollama LLM or the Claude API, and emits structured results.

Signals:
  research_complete(dict) — keys: symbol, short_term, long_term, catalysts,
                                   sentiment, summary, timestamp, source
  research_error(str)     — human-readable error message
"""
import difflib
import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import feedparser
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
        yf_articles: List[Dict[str, str]] = []
        for item in raw_news:
            try:
                pub_ts = item.get("providerPublishTime") or item.get("pubDate")
                if isinstance(pub_ts, (int, float)):
                    pub_dt = datetime.fromtimestamp(pub_ts)
                else:
                    pub_dt = datetime.now()
                if pub_dt >= cutoff:
                    yf_articles.append({
                        "date":      pub_dt.strftime("%Y-%m-%d"),
                        "title":     item.get("title", ""),
                        "publisher": item.get("publisher", ""),
                    })
            except Exception:
                continue
            if len(yf_articles) >= 10:
                break

        # 3. Fetch Google News and merge with yfinance articles
        google_articles = self._fetch_google_news(self._symbol)
        articles = self._merge_news_sources(yf_articles, google_articles)

        if not articles:
            self.research_error.emit(
                f"No news found for {self._symbol} in the last 7 days.\n"
                "Try again later or check the ticker symbol."
            )
            return

        if not self._running:
            return

        # 4. Fetch additional data from ticker
        price_volume_data = self._fetch_price_volume_data(ticker)
        analyst_data = self._fetch_analyst_ratings(ticker)
        insider_data = self._fetch_insider_transactions(ticker)

        if not self._running:
            return

        # 5. Build prompt
        prompt = self._build_prompt(
            articles, price_volume_data, analyst_data, insider_data
        )

        # 6. Call LLM
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

        # 7. Parse response
        result = self._parse_response(raw_response)
        result["symbol"]    = self._symbol
        result["timestamp"] = datetime.now().isoformat()
        result["source"]    = provider

        # 8. Cache and emit
        save_entry(self._symbol, result)
        self.research_complete.emit(result)

    # ── Data fetchers ──────────────────────────────────────────────────────────

    def _fetch_google_news(self, symbol: str, max_articles: int = 10) -> List[Dict[str, str]]:
        """Fetch recent news from Google News RSS for the given symbol."""
        try:
            query = urllib.parse.quote(f"{symbol} stock")
            url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
            feed = feedparser.parse(url)
            cutoff = datetime.now() - timedelta(days=7)
            articles: List[Dict[str, str]] = []
            for entry in feed.entries[:max_articles * 2]:  # fetch extra to account for filtering
                try:
                    pub_dt = datetime(*entry.published_parsed[:6])
                    if pub_dt < cutoff:
                        continue
                    articles.append({
                        "date": pub_dt.strftime("%Y-%m-%d"),
                        "title": entry.get("title", ""),
                        "publisher": entry.get("source", {}).get("title", "Google News"),
                    })
                except Exception:
                    continue
                if len(articles) >= max_articles:
                    break
            return articles
        except Exception:
            return []

    def _fetch_price_volume_data(self, ticker) -> Dict[str, Any]:
        """Extract price and volume metrics from ticker.info."""
        try:
            info = ticker.info or {}
            data: Dict[str, Any] = {}
            for key in ("fiftyTwoWeekLow", "fiftyTwoWeekHigh", "averageVolume",
                        "regularMarketChangePercent", "currentPrice"):
                val = info.get(key)
                if val is not None:
                    data[key] = val
            return data
        except Exception:
            return {}

    def _fetch_analyst_ratings(self, ticker) -> Dict[str, Any]:
        """Fetch analyst price targets and recommendation summary."""
        data: Dict[str, Any] = {}
        try:
            targets = ticker.analyst_price_targets
            if targets is not None:
                data["targets"] = {
                    "low": getattr(targets, "low", None),
                    "current": getattr(targets, "current", None),
                    "high": getattr(targets, "high", None),
                    "mean": getattr(targets, "mean", None),
                }
        except Exception:
            pass
        try:
            recs = ticker.recommendations_summary
            if recs is not None and not recs.empty:
                row = recs.iloc[0]
                data["recommendations"] = {
                    col: int(row[col]) for col in recs.columns
                    if col != "period" and row[col] is not None
                }
        except Exception:
            pass
        return data

    def _fetch_insider_transactions(self, ticker, max_transactions: int = 10) -> List[Dict[str, Any]]:
        """Fetch recent insider transactions."""
        try:
            df = ticker.insider_transactions
            if df is None or df.empty:
                return []
            transactions: List[Dict[str, Any]] = []
            for _, row in df.head(max_transactions).iterrows():
                transactions.append({
                    "insider": str(row.get("Insider", row.get("insider", ""))),
                    "transaction": str(row.get("Transaction", row.get("transaction", ""))),
                    "shares": row.get("Shares", row.get("shares", "")),
                    "value": row.get("Value", row.get("value", "")),
                })
            return transactions
        except Exception:
            return []

    def _merge_news_sources(
        self, yf_articles: List[Dict[str, str]], google_articles: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        """Merge yfinance and Google News articles, dedup by title similarity, cap at 20."""
        merged = list(yf_articles)  # yfinance takes priority
        yf_titles = [a["title"].lower() for a in yf_articles]

        for g_article in google_articles:
            g_title = g_article["title"].lower()
            is_dup = any(
                difflib.SequenceMatcher(None, g_title, yt).ratio() >= 0.85
                for yt in yf_titles
            )
            if not is_dup:
                merged.append(g_article)
                yf_titles.append(g_title)
            if len(merged) >= 20:
                break
        return merged[:20]

    # ── Prompt builder ─────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        articles: list,
        price_volume_data: Dict[str, Any],
        analyst_data: Dict[str, Any],
        insider_data: List[Dict[str, Any]],
    ) -> str:
        news_lines = "\n".join(
            f"- [{a['date']}] {a['title']} ({a['publisher']})"
            for a in articles
        )

        fundamentals = self._format_fundamentals_section()
        price_volume = self._format_price_volume_section(price_volume_data)
        analyst = self._format_analyst_section(analyst_data)
        insider = self._format_insider_section(insider_data)

        return (
            f"/no_think\n\n"
            f"You are a stock research analyst. Analyze recent news and data about "
            f"{self._symbol} and provide a concise investment outlook.\n\n"
            f"=== RECENT NEWS (last 7 days) ===\n{news_lines}\n\n"
            f"=== FUNDAMENTALS ===\n{fundamentals}\n\n"
            f"=== PRICE & VOLUME ===\n{price_volume}\n\n"
            f"=== ANALYST RATINGS ===\n{analyst}\n\n"
            f"=== INSIDER TRADING ===\n{insider}\n\n"
            f"Please respond in this EXACT format (one line per field):\n"
            f"SHORT_TERM: <1-2 sentence outlook for the next 1-4 weeks>\n"
            f"LONG_TERM: <1-2 sentence outlook for the next 6-18 months>\n"
            f"CATALYSTS: <comma-separated list of key near-term catalysts>\n"
            f"SENTIMENT: <exactly one of: BULLISH, BEARISH, NEUTRAL>\n"
            f"SUMMARY: <2-3 sentence overall summary>\n"
        )

    def _format_fundamentals_section(self) -> str:
        r = self._scan_result
        if r is None:
            return "- (No fundamentals available)"
        parts = []
        if r.sector:
            parts.append(f"Sector: {r.sector}")
        if r.pe_ratio is not None:
            parts.append(f"P/E: {r.pe_ratio:.1f}")
        if r.peg_ratio is not None:
            parts.append(f"PEG Ratio: {r.peg_ratio:.2f}")
        if r.rsi is not None:
            parts.append(f"RSI: {r.rsi:.0f}")
        parts.append(f"Total Score: {r.total_score:.1f}")
        if r.revenue_growth is not None:
            parts.append(f"Revenue Growth: {r.revenue_growth * 100:.1f}%")
        if r.roe is not None:
            parts.append(f"ROE: {r.roe * 100:.1f}%")
        if r.debt_equity is not None:
            parts.append(f"Debt/Equity: {r.debt_equity:.2f}")
        if r.free_cash_flow is not None:
            fcf = r.free_cash_flow
            if abs(fcf) >= 1e9:
                parts.append(f"Free Cash Flow: ${fcf / 1e9:.2f}B")
            elif abs(fcf) >= 1e6:
                parts.append(f"Free Cash Flow: ${fcf / 1e6:.1f}M")
            else:
                parts.append(f"Free Cash Flow: ${fcf:,.0f}")
        return "\n".join(f"- {p}" for p in parts) if parts else "- (No fundamentals available)"

    def _format_price_volume_section(self, data: Dict[str, Any]) -> str:
        if not data:
            return "- No data available"
        parts = []
        low52 = data.get("fiftyTwoWeekLow")
        high52 = data.get("fiftyTwoWeekHigh")
        if low52 is not None and high52 is not None:
            parts.append(f"52-Week Range: ${low52:.2f} - ${high52:.2f}")
        elif low52 is not None:
            parts.append(f"52-Week Low: ${low52:.2f}")
        elif high52 is not None:
            parts.append(f"52-Week High: ${high52:.2f}")
        price = data.get("currentPrice")
        if price is not None:
            parts.append(f"Current Price: ${price:.2f}")
        change_pct = data.get("regularMarketChangePercent")
        if change_pct is not None:
            parts.append(f"Change: {change_pct:+.2f}%")
        avg_vol = data.get("averageVolume")
        if avg_vol is not None:
            if avg_vol >= 1e6:
                parts.append(f"Avg Volume: {avg_vol / 1e6:.1f}M")
            else:
                parts.append(f"Avg Volume: {avg_vol:,.0f}")
        return "\n".join(f"- {p}" for p in parts) if parts else "- No data available"

    def _format_analyst_section(self, data: Dict[str, Any]) -> str:
        if not data:
            return "- No data available"
        parts = []
        targets = data.get("targets", {})
        if targets:
            t_parts = []
            if targets.get("low") is not None:
                t_parts.append(f"Low: ${targets['low']:.2f}")
            if targets.get("mean") is not None:
                t_parts.append(f"Mean: ${targets['mean']:.2f}")
            if targets.get("high") is not None:
                t_parts.append(f"High: ${targets['high']:.2f}")
            if targets.get("current") is not None:
                t_parts.append(f"Current: ${targets['current']:.2f}")
            if t_parts:
                parts.append(f"Price Targets — {', '.join(t_parts)}")
        recs = data.get("recommendations", {})
        if recs:
            rec_parts = [f"{k}: {v}" for k, v in recs.items()]
            parts.append(f"Recommendations — {', '.join(rec_parts)}")
            total = sum(recs.values())
            if total > 0:
                buy_keys = ("strongBuy", "buy")
                bullish = sum(recs.get(k, 0) for k in buy_keys)
                parts.append(f"Bullish %: {bullish / total * 100:.0f}%")
        return "\n".join(f"- {p}" for p in parts) if parts else "- No data available"

    def _format_insider_section(self, data: List[Dict[str, Any]]) -> str:
        if not data:
            return "- No data available"
        buys = 0
        sells = 0
        top_by_value: List[Dict[str, Any]] = []
        for txn in data:
            txn_type = str(txn.get("transaction", "")).lower()
            if "purchase" in txn_type or "buy" in txn_type:
                buys += 1
            elif "sale" in txn_type or "sell" in txn_type:
                sells += 1
            try:
                val = txn.get("value")
                if val is not None:
                    numeric_val = abs(float(str(val).replace(",", "").replace("$", "")))
                else:
                    numeric_val = 0
            except (ValueError, TypeError):
                numeric_val = 0
            top_by_value.append({**txn, "_numeric_value": numeric_val})

        parts = [f"Recent insider transactions: {buys} buys, {sells} sells"]
        top_by_value.sort(key=lambda x: x["_numeric_value"], reverse=True)
        for txn in top_by_value[:3]:
            val = txn.get("value", "N/A")
            parts.append(
                f"  {txn.get('insider', 'Unknown')} — {txn.get('transaction', '?')}, "
                f"shares: {txn.get('shares', '?')}, value: {val}"
            )
        return "\n".join(f"- {p}" for p in parts)

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
                "Go to Settings -> AI and enter your Anthropic API key."
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
