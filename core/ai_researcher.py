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
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import logging

import feedparser
import yfinance as yf
from PyQt5.QtCore import QThread, pyqtSignal

from core.ai_research_store import get_cached_entry, save_entry
from core.scan_result import ScanResult

_log = logging.getLogger(__name__)


class AIResearcher(QThread):
    research_complete = pyqtSignal(dict)
    research_error    = pyqtSignal(str)
    research_status   = pyqtSignal(str)   # intermediate status updates (e.g. "Fetching news…")

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
                _log.info("AI cache hit for %s — skipping LLM call", self._symbol)
                self.research_complete.emit(cached)
                return

        _log.info("AI research started for %s", self._symbol)
        self.research_status.emit(f"Fetching news for {self._symbol}…")

        # 2. Fetch news via yfinance
        try:
            ticker = yf.Ticker(self._symbol)
            raw_news = ticker.news or []
        except Exception as exc:
            _log.error("Failed to fetch news for %s: %s", self._symbol, exc)
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
            _log.warning("No news found for %s in the last 7 days", self._symbol)
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
        options_data = self._fetch_options_data(ticker)
        short_interest_data = self._fetch_short_interest(ticker)
        earnings_history = self._fetch_earnings_history(ticker)

        if not self._running:
            return

        # 4b. Fetch macro/sector news and congressional trades
        sector = self._scan_result.sector if self._scan_result else None
        macro_articles = self._fetch_macro_news(self._symbol, sector)
        congressional_data = self._fetch_congressional_trades(self._symbol)

        if not self._running:
            return

        # 5. Build prompt
        prompt = self._build_prompt(
            articles, price_volume_data, analyst_data, insider_data,
            options_data, macro_articles, short_interest_data,
            earnings_history, congressional_data,
        )

        # 6. Call LLM
        provider = self._settings.get("ai_provider", "ollama")
        model = self._settings.get(
            "ai_ollama_model" if provider == "ollama" else
            "ai_claude_model" if provider == "claude" else
            "ai_openrouter_model",
            "?"
        )
        _log.info("Calling %s (%s) for %s", provider, model, self._symbol)
        self.research_status.emit(f"Calling {provider} ({model})…")
        try:
            if provider == "claude":
                raw_response = self._call_claude(prompt)
            elif provider == "openrouter":
                raw_response = self._call_openrouter(prompt)
            else:
                raw_response = self._call_ollama(prompt)
        except Exception as exc:
            _log.error("LLM call failed for %s [%s]: %s", self._symbol, provider, exc)
            self.research_error.emit(str(exc))
            return

        if not self._running:
            return

        # 7. Parse response
        result = self._parse_response(raw_response)
        result["symbol"]    = self._symbol
        result["timestamp"] = datetime.now().isoformat()
        result["source"]    = provider

        # 8. Validate — don't cache an empty response
        if not any(result.get(k) for k in ("short_term", "long_term", "summary")):
            _log.warning(
                "LLM returned empty/unparseable response for %s — not caching", self._symbol
            )
            self.research_error.emit(
                f"AI returned an empty response for {self._symbol}. Try refreshing."
            )
            return

        # 9. Cache and emit
        _log.info(
            "AI research complete for %s — sentiment=%s direction=%s",
            self._symbol, result.get("sentiment"), result.get("direction"),
        )
        self.research_status.emit(f"✓ Done")
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

    def _fetch_options_data(self, ticker) -> Dict[str, Any]:
        """Fetch options chain for nearest 2 expiry dates."""
        try:
            expiry_dates = ticker.options
            if not expiry_dates:
                return {}
            dates_to_fetch = expiry_dates[:2]
            data: Dict[str, Any] = {
                "expiries": [],
                "total_call_oi": 0,
                "total_put_oi": 0,
                "iv_values": [],
            }
            for exp_date in dates_to_fetch:
                try:
                    chain = ticker.option_chain(exp_date)
                except Exception:
                    continue
                calls = chain.calls
                puts = chain.puts

                call_oi = int(calls["openInterest"].sum()) if "openInterest" in calls.columns else 0
                put_oi = int(puts["openInterest"].sum()) if "openInterest" in puts.columns else 0
                data["total_call_oi"] += call_oi
                data["total_put_oi"] += put_oi

                # Collect IV values for averaging
                if "impliedVolatility" in calls.columns:
                    data["iv_values"].extend(calls["impliedVolatility"].dropna().tolist())
                if "impliedVolatility" in puts.columns:
                    data["iv_values"].extend(puts["impliedVolatility"].dropna().tolist())

                # Top 5 calls/puts by open interest
                top_calls = []
                if "openInterest" in calls.columns and not calls.empty:
                    top_c = calls.nlargest(5, "openInterest")
                    for _, row in top_c.iterrows():
                        top_calls.append({
                            "strike": row.get("strike"),
                            "oi": int(row.get("openInterest", 0)),
                            "volume": int(row.get("volume", 0)) if row.get("volume") is not None else 0,
                            "iv": round(row.get("impliedVolatility", 0) * 100, 1),
                        })
                top_puts = []
                if "openInterest" in puts.columns and not puts.empty:
                    top_p = puts.nlargest(5, "openInterest")
                    for _, row in top_p.iterrows():
                        top_puts.append({
                            "strike": row.get("strike"),
                            "oi": int(row.get("openInterest", 0)),
                            "volume": int(row.get("volume", 0)) if row.get("volume") is not None else 0,
                            "iv": round(row.get("impliedVolatility", 0) * 100, 1),
                        })

                data["expiries"].append({
                    "date": exp_date,
                    "call_oi": call_oi,
                    "put_oi": put_oi,
                    "top_calls": top_calls,
                    "top_puts": top_puts,
                })

            # Compute averages
            if data["iv_values"]:
                data["avg_iv"] = round(sum(data["iv_values"]) / len(data["iv_values"]) * 100, 1)
            else:
                data["avg_iv"] = None
            del data["iv_values"]

            if data["total_call_oi"] > 0:
                data["put_call_ratio"] = round(data["total_put_oi"] / data["total_call_oi"], 2)
            else:
                data["put_call_ratio"] = None

            return data
        except Exception:
            return {}

    def _fetch_short_interest(self, ticker) -> Dict[str, Any]:
        """Fetch short interest metrics from ticker.info."""
        try:
            info = ticker.info or {}
            data: Dict[str, Any] = {}
            short_ratio = info.get("shortRatio")
            short_float = info.get("shortPercentOfFloat")
            shares_short = info.get("sharesShort")
            if short_ratio is not None:
                data["days_to_cover"] = round(float(short_ratio), 1)
            if short_float is not None:
                data["short_float_pct"] = round(float(short_float) * 100, 1)
            if shares_short is not None:
                data["shares_short"] = int(shares_short)
            return data
        except Exception:
            return {}

    def _fetch_earnings_history(self, ticker) -> List[Dict[str, Any]]:
        """Fetch last 4 quarters of earnings surprises."""
        try:
            df = ticker.earnings_dates
            if df is None or df.empty:
                return []
            records: List[Dict[str, Any]] = []
            for dt, row in df.head(8).iterrows():
                eps_est = row.get("EPS Estimate")
                eps_act = row.get("Reported EPS")
                surprise_pct = row.get("Surprise(%)")
                # Skip future estimates (no actuals yet)
                if eps_act is None or (isinstance(eps_act, float) and eps_act != eps_act):
                    continue
                record: Dict[str, Any] = {"date": str(dt)[:10]}
                if eps_est is not None and not (isinstance(eps_est, float) and eps_est != eps_est):
                    record["eps_estimate"] = round(float(eps_est), 2)
                record["eps_actual"] = round(float(eps_act), 2)
                if surprise_pct is not None and not (isinstance(surprise_pct, float) and surprise_pct != surprise_pct):
                    record["surprise_pct"] = round(float(surprise_pct), 1)
                    record["beat"] = float(surprise_pct) > 0
                records.append(record)
                if len(records) >= 4:
                    break
            return records
        except Exception:
            return []

    def _fetch_congressional_trades(self, symbol: str, days: int = 180) -> List[Dict[str, Any]]:
        """Fetch House + Senate congressional trades filtered to tracked politicians."""
        tracked = [p.strip() for p in
                   self._settings.get("congressional_tracked_politicians", "").split(",")
                   if p.strip()]
        house  = self._fetch_house_trades(symbol, tracked, days)
        senate = self._fetch_senate_trades(symbol, tracked, days)
        merged = house + senate
        merged.sort(key=lambda x: x.get("date", ""), reverse=True)
        return merged[:20]

    @staticmethod
    def _politician_matches(name: str, tracked: List[str]) -> bool:
        """Return True if `name` matches any entry in the tracked list."""
        name_lower = name.lower()
        for politician in tracked:
            pol_lower = politician.lower()
            if pol_lower in name_lower:
                return True
            # Handle reversed format "LastName, FirstName" vs "FirstName LastName"
            words = pol_lower.split()
            if len(words) >= 2 and all(w in name_lower for w in words):
                return True
        return False

    def _load_json_cache(self, cache_path: str, url: str) -> Optional[list]:
        """Load a JSON list from a 24h local cache, downloading from url if stale."""
        import urllib.request as _req
        try:
            if os.path.exists(cache_path):
                if time.time() - os.path.getmtime(cache_path) < 86400:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        return json.load(f)
        except Exception:
            pass
        try:
            req = _req.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _req.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return data
        except Exception:
            return None

    def _fetch_house_trades(
        self, symbol: str, tracked: List[str], days: int
    ) -> List[Dict[str, Any]]:
        """Fetch House trades from House Stock Watcher S3."""
        cache_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "data", "house_trades_cache.json")
        )
        url = ("https://house-stock-watcher-data.s3-us-west-2.amazonaws.com"
               "/data/all_transactions.json")
        all_trades = self._load_json_cache(cache_path, url)
        if not all_trades:
            return []

        cutoff   = datetime.now() - timedelta(days=days)
        sym_upper = symbol.upper()
        matches: List[Dict[str, Any]] = []
        for trade in all_trades:
            ticker = str(trade.get("ticker", "")).strip().upper().replace(".", "-")
            if ticker != sym_upper or ticker in ("", "--"):
                continue
            rep = trade.get("representative", "")
            if tracked and not self._politician_matches(rep, tracked):
                continue
            tx_date_str = str(trade.get("transaction_date", ""))[:10]
            try:
                if datetime.strptime(tx_date_str, "%Y-%m-%d") < cutoff:
                    continue
            except Exception:
                pass
            matches.append({
                "date":           tx_date_str,
                "representative": rep,
                "type":           trade.get("type", ""),
                "amount":         trade.get("amount", ""),
                "district":       trade.get("district", ""),
                "chamber":        "House",
            })
        return matches

    def _fetch_senate_trades(
        self, symbol: str, tracked: List[str], days: int
    ) -> List[Dict[str, Any]]:
        """Fetch Senate trades from Senate Stock Watcher S3."""
        cache_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "data", "senate_trades_cache.json")
        )
        url = ("https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com"
               "/aggregate/all_transactions.json")
        all_trades = self._load_json_cache(cache_path, url)
        if not all_trades:
            return []

        cutoff    = datetime.now() - timedelta(days=days)
        sym_upper = symbol.upper()
        matches: List[Dict[str, Any]] = []
        for trade in all_trades:
            ticker = str(trade.get("ticker", "")).strip().upper().replace(".", "-")
            if ticker != sym_upper or ticker in ("", "--"):
                continue
            # Senator name may be "LastName, FirstName" or a dict with first/last
            senator_raw = trade.get("senator", "")
            if isinstance(senator_raw, dict):
                senator = (f"{senator_raw.get('first_name', '')} "
                           f"{senator_raw.get('last_name', '')}").strip()
            else:
                senator = str(senator_raw)
            if tracked and not self._politician_matches(senator, tracked):
                continue
            tx_date_str = str(trade.get("transaction_date", ""))[:10]
            try:
                if datetime.strptime(tx_date_str, "%Y-%m-%d") < cutoff:
                    continue
            except Exception:
                pass
            matches.append({
                "date":           tx_date_str,
                "representative": senator,
                "type":           trade.get("type", ""),
                "amount":         trade.get("amount", ""),
                "district":       trade.get("state", ""),
                "chamber":        "Senate",
            })
        return matches

    def _fetch_macro_news(self, symbol: str, sector: Optional[str]) -> List[Dict[str, str]]:
        """Fetch macro/sector news from Google News RSS for broader context."""
        queries = []
        if sector:
            queries.append(f"{sector} sector market outlook")
        queries.append("stock market geopolitical")

        cutoff = datetime.now() - timedelta(days=7)
        articles: List[Dict[str, str]] = []
        seen_titles: List[str] = []

        for query in queries:
            try:
                encoded = urllib.parse.quote(query)
                url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
                feed = feedparser.parse(url)
                for entry in feed.entries[:15]:
                    try:
                        pub_dt = datetime(*entry.published_parsed[:6])
                        if pub_dt < cutoff:
                            continue
                        title = entry.get("title", "")
                        # Dedup by similarity
                        is_dup = any(
                            difflib.SequenceMatcher(None, title.lower(), st).ratio() >= 0.85
                            for st in seen_titles
                        )
                        if is_dup:
                            continue
                        articles.append({
                            "date": pub_dt.strftime("%Y-%m-%d"),
                            "title": title,
                            "publisher": entry.get("source", {}).get("title", "Google News"),
                        })
                        seen_titles.append(title.lower())
                    except Exception:
                        continue
                    if len(articles) >= 10:
                        break
            except Exception:
                continue
            if len(articles) >= 10:
                break

        return articles[:10]

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
        options_data: Optional[Dict[str, Any]] = None,
        macro_articles: Optional[List[Dict[str, str]]] = None,
        short_interest_data: Optional[Dict[str, Any]] = None,
        earnings_history: Optional[List[Dict[str, Any]]] = None,
        congressional_data: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        news_lines = "\n".join(
            f"- [{a['date']}] {a['title']} ({a['publisher']})"
            for a in articles
        )

        fundamentals = self._format_fundamentals_section()
        price_volume = self._format_price_volume_section(price_volume_data)
        analyst = self._format_analyst_section(analyst_data)
        insider = self._format_insider_section(insider_data)
        options = self._format_options_section(options_data or {})
        macro = self._format_macro_news_section(macro_articles or [])
        short_interest = self._format_short_interest_section(short_interest_data or {})
        earnings = self._format_earnings_history_section(earnings_history or [])
        congressional = self._format_congressional_section(congressional_data or [])

        return (
            f"/no_think\n\n"
            f"You are a stock research analyst advising a trader who uses debit spreads "
            f"and put spreads. Analyze recent news, data, and the options chain for "
            f"{self._symbol} and provide a concise investment outlook with actionable strategies.\n\n"
            f"=== RECENT NEWS (last 7 days) ===\n{news_lines}\n\n"
            f"=== MACRO/SECTOR NEWS ===\n{macro}\n\n"
            f"=== FUNDAMENTALS ===\n{fundamentals}\n\n"
            f"=== PRICE & VOLUME ===\n{price_volume}\n\n"
            f"=== SHORT INTEREST ===\n{short_interest}\n\n"
            f"=== EARNINGS HISTORY (last 4 quarters) ===\n{earnings}\n\n"
            f"=== ANALYST RATINGS ===\n{analyst}\n\n"
            f"=== INSIDER TRADING ===\n{insider}\n\n"
            f"=== CONGRESSIONAL TRADES (STOCK Act disclosures) ===\n{congressional}\n\n"
            f"=== OPTIONS CHAIN ===\n{options}\n\n"
            f"Please respond in this EXACT format (one line per field):\n"
            f"SHORT_TERM: <1-2 sentence outlook for the next 1-4 weeks>\n"
            f"LONG_TERM: <1-2 sentence outlook for the next 6-18 months>\n"
            f"CATALYSTS: <comma-separated list of key near-term catalysts>\n"
            f"SENTIMENT: <exactly one of: BULLISH, BEARISH, NEUTRAL>\n"
            f"DIRECTION: <exactly one of: UP, DOWN, SIDEWAYS>\n"
            f"TIMEFRAME: <expected timeframe for the move, e.g. \"2-4 weeks\", \"1-3 months\">\n"
            f"CONGRESSIONAL_SIGNAL: <BULLISH, BEARISH, NEUTRAL, or NONE — 1 sentence on what congressional buying/selling implies>\n"
            f"STOCK_STRATEGY: <1-3 sentence actionable stock strategy with entry/exit reasoning>\n"
            f"OPTIONS_STRATEGY: <1-3 sentence options strategy — suggest specific spread types, "
            f"approximate strikes/expiries based on the chain data, highlight good risk/reward setups>\n"
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

    def _format_options_section(self, data: Dict[str, Any]) -> str:
        if not data or not data.get("expiries"):
            return "- No options data available"
        parts = []
        pcr = data.get("put_call_ratio")
        if pcr is not None:
            parts.append(f"Put/Call Ratio: {pcr}")
        avg_iv = data.get("avg_iv")
        if avg_iv is not None:
            parts.append(f"Average IV: {avg_iv}%")
        parts.append(f"Total Call OI: {data.get('total_call_oi', 0):,}  |  Total Put OI: {data.get('total_put_oi', 0):,}")

        for exp in data["expiries"]:
            parts.append(f"\nExpiry: {exp['date']}  (Call OI: {exp['call_oi']:,}, Put OI: {exp['put_oi']:,})")
            if exp.get("top_calls"):
                parts.append("  Top Calls by OI:")
                for c in exp["top_calls"]:
                    parts.append(f"    Strike ${c['strike']}  OI:{c['oi']:,}  Vol:{c['volume']:,}  IV:{c['iv']}%")
            if exp.get("top_puts"):
                parts.append("  Top Puts by OI:")
                for p in exp["top_puts"]:
                    parts.append(f"    Strike ${p['strike']}  OI:{p['oi']:,}  Vol:{p['volume']:,}  IV:{p['iv']}%")

        return "\n".join(f"- {p}" if not p.startswith(" ") and not p.startswith("\n") else p for p in parts)

    def _format_short_interest_section(self, data: Dict[str, Any]) -> str:
        if not data:
            return "- No data available"
        parts = []
        dtc = data.get("days_to_cover")
        if dtc is not None:
            parts.append(f"Days to Cover: {dtc}")
        sf = data.get("short_float_pct")
        if sf is not None:
            parts.append(f"Short % of Float: {sf}%")
        ss = data.get("shares_short")
        if ss is not None:
            if ss >= 1_000_000:
                parts.append(f"Shares Short: {ss / 1_000_000:.1f}M")
            else:
                parts.append(f"Shares Short: {ss:,}")
        return "\n".join(f"- {p}" for p in parts) if parts else "- No data available"

    def _format_earnings_history_section(self, records: List[Dict[str, Any]]) -> str:
        if not records:
            return "- No earnings history available"
        parts = []
        for r in records:
            est = r.get("eps_estimate")
            act = r.get("eps_actual")
            surprise = r.get("surprise_pct")
            beat = r.get("beat")
            line = f"[{r['date']}] Actual EPS: {act}"
            if est is not None:
                line += f"  Est: {est}"
            if surprise is not None:
                icon = "BEAT" if beat else "MISS"
                line += f"  Surprise: {surprise:+.1f}% ({icon})"
            parts.append(line)
        return "\n".join(f"- {p}" for p in parts)

    def _format_congressional_section(self, trades: List[Dict[str, Any]]) -> str:
        if not trades:
            return "- No trades found for tracked politicians in the last 180 days (House + Senate)"
        buys  = [t for t in trades if "purchase" in t.get("type", "").lower()]
        sells = [t for t in trades if "sale" in t.get("type", "").lower()]
        parts = [
            f"Tracked-politician trades (House + Senate): "
            f"{len(buys)} purchase(s), {len(sells)} sale(s) in last 180 days"
        ]
        for t in trades[:8]:
            chamber = t.get("chamber", "")
            chamber_tag = f"[{chamber}] " if chamber else ""
            parts.append(
                f"  [{t['date']}] {chamber_tag}{t['representative']} ({t['district']}) — "
                f"{t['type'].upper()} {t['amount']}"
            )
        return "\n".join(f"- {p}" if not p.startswith("  ") else p for p in parts)

    def _format_macro_news_section(self, articles: List[Dict[str, str]]) -> str:
        if not articles:
            return "- No macro/sector news available"
        lines = []
        for a in articles:
            lines.append(f"- [{a['date']}] {a['title']} ({a['publisher']})")
        return "\n".join(lines)

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
        _MAX_RETRIES = 1
        for attempt in range(_MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    return body.get("response", "")
            except Exception as exc:
                is_timeout = (
                    "timed out" in str(exc).lower()
                    or "urlopen error" in str(exc).lower()
                )
                if is_timeout and attempt < _MAX_RETRIES:
                    self.research_status.emit(
                        f"Ollama timed out — retrying (attempt {attempt + 2}/{_MAX_RETRIES + 1})…"
                    )
                    _log.warning("Ollama timeout for %s, retrying…", self._symbol)
                    continue
                raise RuntimeError(
                    f"Ollama request failed: {exc}\n\n"
                    "Make sure Ollama is running and the model is pulled:\n"
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
            "max_tokens": 1024,
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

    def _call_openrouter(self, prompt: str) -> str:
        import urllib.request
        api_key = self._settings.get("ai_openrouter_api_key", "")
        model   = self._settings.get("ai_openrouter_model", "qwen/qwen3-coder:free")
        if not api_key:
            raise RuntimeError(
                "OpenRouter API key is not set.\n"
                "Go to Settings -> AI and enter your OpenRouter API key."
            )
        # Strip /no_think prefix — it's Qwen3-Ollama-specific and not needed via API
        clean_prompt = prompt.lstrip()
        if clean_prompt.startswith("/no_think"):
            clean_prompt = clean_prompt[len("/no_think"):].lstrip()

        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": clean_prompt}],
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                choices = body.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
                return ""
        except Exception as exc:
            raise RuntimeError(f"OpenRouter request failed: {exc}") from exc

    # ── Response parser ───────────────────────────────────────────────────────

    def _parse_response(self, text: str) -> Dict[str, str]:
        # Strip Qwen3 extended-thinking blocks before tag scan
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        result: Dict[str, str] = {
            "short_term":           "",
            "long_term":            "",
            "catalysts":            "",
            "sentiment":            "NEUTRAL",
            "direction":            "SIDEWAYS",
            "timeframe":            "",
            "congressional_signal": "NONE",
            "stock_strategy":       "",
            "options_strategy":     "",
            "summary":              "",
        }
        tag_map = {
            "SHORT_TERM:":           "short_term",
            "LONG_TERM:":            "long_term",
            "CATALYSTS:":            "catalysts",
            "SENTIMENT:":            "sentiment",
            "DIRECTION:":            "direction",
            "TIMEFRAME:":            "timeframe",
            "CONGRESSIONAL_SIGNAL:": "congressional_signal",
            "STOCK_STRATEGY:":       "stock_strategy",
            "OPTIONS_STRATEGY:":     "options_strategy",
            "SUMMARY:":              "summary",
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

        # Normalise direction
        direction = result["direction"].upper()
        if "UP" in direction:
            result["direction"] = "UP"
        elif "DOWN" in direction:
            result["direction"] = "DOWN"
        else:
            result["direction"] = "SIDEWAYS"

        # If parsing failed entirely, store the raw text in summary
        if not any(result[k] for k in ("short_term", "long_term", "summary", "congressional_signal")):
            result["summary"] = text.strip()

        return result
