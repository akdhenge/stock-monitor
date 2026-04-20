import json
import logging
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from core.ai_research_store import get_cached_entry
from core.scan_result import ScanResult

_log = logging.getLogger(__name__)

# Estimated cost per million tokens (USD) — update if Anthropic changes pricing
_COST_PER_M: Dict[str, Dict[str, float]] = {
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-7":            {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-haiku-20240307":      {"input": 0.25,  "output": 1.25},
}
_DEFAULT_COST = {"input": 3.00, "output": 15.00}


class ClaudeRankingAnalyst(QThread):
    """Feeds top-10 scan results + cached AI research to Claude for portfolio ranking."""

    ranking_complete = pyqtSignal(dict)
    ranking_error    = pyqtSignal(str)
    ranking_status   = pyqtSignal(str)

    def __init__(
        self,
        scan_results: List[ScanResult],
        get_settings: Callable[[], Dict[str, Any]],
        trigger: str = "desktop",
        parent=None,
    ):
        super().__init__(parent)
        self._scan_results = scan_results[:10]
        self._get_settings = get_settings
        self._trigger = trigger  # "desktop" or "web"

    # ── Thread entry ──────────────────────────────────────────────────────────

    def run(self) -> None:
        try:
            self.ranking_status.emit("Assembling stock data…")
            enriched = self._enrich_results()

            self.ranking_status.emit("Building prompt…")
            prompt = self._build_prompt(enriched)

            self.ranking_status.emit("Calling Claude API…")
            raw_text, input_tokens, output_tokens = self._call_claude(prompt)

            self.ranking_status.emit("Parsing response…")
            result = self._parse_response(raw_text)

            settings = self._get_settings()
            model = settings.get("claude_ranking_model", "claude-sonnet-4-6")
            rates = _COST_PER_M.get(model, _DEFAULT_COST)
            cost_usd = (input_tokens / 1_000_000) * rates["input"] + \
                       (output_tokens / 1_000_000) * rates["output"]

            result["input_tokens"]  = input_tokens
            result["output_tokens"] = output_tokens
            result["cost_usd"]      = round(cost_usd, 4)
            result["generated_at"]  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            result["model"]         = model

            self._log_usage(model, input_tokens, output_tokens, cost_usd)
            self.ranking_status.emit("Saving & publishing…")
            self._save_cache(result)
            self._publish_to_r2(result)

            self.ranking_complete.emit(result)

        except Exception as exc:
            _log.error("ClaudeRankingAnalyst error: %s", exc)
            self.ranking_error.emit(str(exc))

    # ── Data assembly ─────────────────────────────────────────────────────────

    def _enrich_results(self) -> List[dict]:
        enriched = []
        for sr in self._scan_results:
            entry: dict = {
                "symbol":           sr.symbol,
                "total_score":      sr.total_score,
                "sector":           sr.sector or "—",
                "score_value":      sr.score_value,
                "score_growth":     sr.score_growth,
                "score_technical":  sr.score_technical,
                "pe_ratio":         sr.pe_ratio,
                "peg_ratio":        sr.peg_ratio,
                "rsi":              sr.rsi,
                "roe":              sr.roe,
                "revenue_growth":   sr.revenue_growth,
                "debt_equity":      sr.debt_equity,
                "macd_bullish":     sr.macd_bullish,
                "near_200d_ma":     sr.near_200d_ma,
            }
            cached = get_cached_entry(sr.symbol)
            if cached:
                entry.update({
                    "sentiment":            cached.get("sentiment", ""),
                    "direction":            cached.get("direction", ""),
                    "short_term":           cached.get("short_term", ""),
                    "long_term":            cached.get("long_term", ""),
                    "catalysts":            cached.get("catalysts", ""),
                    "congressional_signal": cached.get("congressional_signal", ""),
                    "stock_strategy":       cached.get("stock_strategy", ""),
                    "options_strategy":     cached.get("options_strategy", ""),
                    "summary":              cached.get("summary", ""),
                    "ai_research":          True,
                })
            else:
                entry["ai_research"] = False
            enriched.append(entry)
        return enriched

    # ── Prompt builder ────────────────────────────────────────────────────────

    def _build_prompt(self, enriched: List[dict]) -> str:
        lines = [
            "You are a portfolio analyst. Below are up to 10 pre-screened stocks from a quantitative scan,",
            "each with fundamental data and (where available) individual AI research. Your tasks:",
            "",
            "1. Rank them 1–10 (1 = strongest near-term buy opportunity).",
            "2. For each stock: 2-sentence rationale, a concrete stock play (entry price zone / target / stop-loss),",
            "   an options play if applicable (specific spread or contract), and risk level (Low/Medium/High).",
            "3. Suggest portfolio allocation % for the top 5 only (must sum to 100% across those 5).",
            "   Set allocation_pct to null for ranks 6–10.",
            "4. Flag one 'hidden gem' (a lower-ranked stock with outsized upside) if you see one, else null.",
            "5. Write one paragraph of overall market/sector context explaining the macro backdrop.",
            "",
            "--- STOCK DATA ---",
        ]
        for i, s in enumerate(enriched, 1):
            lines.append(f"\n[{i}] {s['symbol']} | Sector: {s['sector']} | Composite Score: {s['total_score']:.1f}")
            lines.append(
                f"    Fundamentals — PE: {s['pe_ratio'] or '—'}  PEG: {s['peg_ratio'] or '—'}  "
                f"ROE: {s['roe'] or '—'}  D/E: {s['debt_equity'] or '—'}  "
                f"Rev Growth: {s['revenue_growth'] or '—'}"
            )
            lines.append(
                f"    Technical   — RSI: {s['rsi'] or '—'}  MACD bullish: {s['macd_bullish']}  "
                f"Near 200d MA: {s['near_200d_ma']}"
            )
            lines.append(
                f"    Sub-scores  — Value: {s['score_value']:.0f}  Growth: {s['score_growth']:.0f}  "
                f"Tech: {s['score_technical']:.0f}"
            )
            if s["ai_research"]:
                lines.append(f"    Sentiment: {s['sentiment']}  Direction: {s['direction']}")
                lines.append(f"    Short-term: {s['short_term']}")
                lines.append(f"    Catalysts:  {s['catalysts']}")
                lines.append(f"    Congressional signal: {s['congressional_signal']}")
                lines.append(f"    Stock strategy: {s['stock_strategy']}")
                lines.append(f"    Options strategy: {s['options_strategy']}")
            else:
                lines.append("    (No individual AI research available — use fundamentals only)")

        lines += [
            "",
            "--- END STOCK DATA ---",
            "",
            'Respond ONLY with a valid JSON object. No markdown fences, no prose before or after the JSON.',
            "Schema:",
            '{',
            '  "ranked": [',
            '    {',
            '      "rank": 1,',
            '      "symbol": "X",',
            '      "rationale": "2-sentence rationale",',
            '      "stock_play": "Entry: $X–Y | Target: $Z | Stop: $W",',
            '      "options_play": "e.g. Bull call spread May $X/$Y or null",',
            '      "risk": "Low|Medium|High",',
            '      "allocation_pct": 25',
            '    }',
            '  ],',
            '  "portfolio_notes": "one paragraph",',
            '  "hidden_gem": "SYMBOL or null"',
            '}',
        ]
        return "\n".join(lines)

    # ── Claude API call ───────────────────────────────────────────────────────

    def _call_claude(self, prompt: str):
        import urllib.request
        settings = self._get_settings()
        api_key = settings.get("ai_claude_api_key", "").strip()
        model   = settings.get("claude_ranking_model", "claude-sonnet-4-6")
        if not api_key:
            raise RuntimeError(
                "Claude API key not set. Go to Settings → AI and enter your Anthropic API key."
            )
        payload = json.dumps({
            "model": model,
            "max_tokens": 3000,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Claude API request failed: {exc}") from exc

        content = body.get("content", [])
        text = content[0].get("text", "") if content and isinstance(content, list) else ""
        usage = body.get("usage", {})
        input_tokens  = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        return text, input_tokens, output_tokens

    # ── Response parser ───────────────────────────────────────────────────────

    def _parse_response(self, raw: str) -> dict:
        text = raw.strip()
        # Strip markdown fences if model wraps anyway
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON object
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])
            else:
                raise RuntimeError(f"Could not parse Claude response as JSON: {text[:300]}")
        if "ranked" not in data:
            raise RuntimeError(f"Response missing 'ranked' key: {text[:300]}")
        return data

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_cache(self, result: dict) -> None:
        path = os.path.join("data", "claude_ranking_cache.json")
        os.makedirs("data", exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            _log.error("ClaudeRankingAnalyst: failed to save cache: %s", exc)

    def _log_usage(self, model: str, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        path = os.path.join("data", "claude_usage_log.json")
        os.makedirs("data", exist_ok=True)
        record = {
            "ts":            datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model":         model,
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      round(cost_usd, 6),
            "trigger":       self._trigger,
        }
        log: list = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    log = json.load(f)
            except Exception:
                log = []
        log.append(record)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            _log.error("ClaudeRankingAnalyst: failed to write usage log: %s", exc)
        _log.info(
            "Claude ranking cost — model=%s in=%d out=%d cost=$%.4f trigger=%s",
            model, input_tokens, output_tokens, cost_usd, self._trigger,
        )

    def _publish_to_r2(self, result: dict) -> None:
        settings = self._get_settings()
        account_id = settings.get("r2_account_id", "").strip()
        access_key = settings.get("r2_access_key_id", "").strip()
        secret_key = settings.get("r2_secret_access_key", "").strip()
        endpoint   = settings.get("r2_endpoint_url", "").strip()
        bucket     = settings.get("r2_bucket", "trader-data")

        if not all([account_id, access_key, secret_key]):
            _log.warning("ClaudeRankingAnalyst: R2 credentials not configured — skipping publish")
            return

        if not endpoint:
            endpoint = f"https://{account_id}.r2.cloudflarestorage.com"

        try:
            import boto3
            from botocore.config import Config
            s3 = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(signature_version="s3v4"),
            )
            payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
            s3.put_object(
                Bucket=bucket,
                Key="ranking.json",
                Body=payload,
                ContentType="application/json",
                CacheControl="no-cache, max-age=0",
            )
            _log.info("ClaudeRankingAnalyst: published ranking.json to R2")
        except ImportError:
            _log.error("ClaudeRankingAnalyst: boto3 not installed — cannot publish to R2")
        except Exception as exc:
            _log.error("ClaudeRankingAnalyst: failed to publish to R2: %s", exc)
