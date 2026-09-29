"""
Thin Finnhub API client for earnings surprise and analyst recommendation data.
Free tier: 60 calls/minute. No caching here — caller is responsible.
"""
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import List, Optional

_log = logging.getLogger(__name__)

_BASE = "https://finnhub.io/api/v1"

# A full-universe screen fires many concurrent Gate 3/4 calls against the free
# tier's 60/min cap — a 429 here silently reads to callers as "no data" (buy_pct
# defaults to 0.0, hard-rejecting Gate 4), not "unknown", so it wrongly rejects
# real candidates rather than just missing an optional signal. Retry with
# backoff instead of giving up immediately, same idiom already used for
# yfinance's flaky .info calls elsewhere in the drawdown pipeline.
_MAX_429_RETRIES = 3
_BACKOFF_BASE_SECS = 2.0


class FinnhubClient:
    def __init__(self, api_key: str):
        self._key = api_key

    def _get(self, path: str, params: dict) -> Optional[dict]:
        params["token"] = self._key
        url = f"{_BASE}{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(_MAX_429_RETRIES + 1):
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    if attempt >= _MAX_429_RETRIES:
                        _log.warning(
                            "Finnhub %s: rate limited (429) — exhausted %d retries",
                            path, _MAX_429_RETRIES,
                        )
                        return None
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = float(retry_after) if retry_after else _BACKOFF_BASE_SECS * (2 ** attempt)
                    except ValueError:
                        delay = _BACKOFF_BASE_SECS * (2 ** attempt)
                    # Jitter — Gate 3/4 run several concurrent worker threads that can all
                    # hit 429 in the same instant; without jitter they'd all retry at the
                    # same instant too and likely re-trigger the same rate limit together.
                    delay += random.uniform(0, 1.0)
                    _log.info(
                        "Finnhub %s: 429 rate limited — retrying in %.1fs (attempt %d/%d)",
                        path, delay, attempt + 1, _MAX_429_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                if exc.code == 403:
                    # Some endpoints (e.g. /stock/upgrade-downgrade) are premium-only on
                    # Finnhub's free tier — this is an expected, already-handled fail-safe
                    # (callers treat a None/[] response as "unknown", not a rejection), not
                    # a broken key. Log quietly so real key/quota problems aren't lost in
                    # the noise of an endpoint we know we can't use.
                    _log.debug("Finnhub %s: 403 (premium-only endpoint, expected)", path)
                else:
                    _log.warning("Finnhub %s failed: %s", path, exc)
                return None
            except Exception as exc:
                _log.warning("Finnhub %s failed: %s", path, exc)
                return None
        return None

    def get_earnings_surprise(self, symbol: str) -> Optional[dict]:
        """Return most recent quarter's earnings surprise data.

        Returns dict with keys: symbol, actual, estimate, period, surprise, surprisePercent
        for EPS. Also fetches revenue surprise if available.
        """
        data = self._get("/stock/earnings", {"symbol": symbol, "limit": 4})
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        # Most recent quarter is first
        return data[0]

    def get_analyst_recommendation(self, symbol: str) -> Optional[dict]:
        """Return most recent month's analyst rating counts.

        Returns dict with keys: buy, hold, sell, strongBuy, strongSell, period, symbol
        """
        data = self._get("/stock/recommendation", {"symbol": symbol})
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        return data[0]

    def get_upgrade_downgrade(self, symbol: str, days: int = 90) -> List[dict]:
        """Return analyst rating change events in last `days` days.
        Each item has keys: action, fromGrade, toGrade, gradeDate, company.
        action='downgrade' for rating cuts."""
        from_date = (date.today() - timedelta(days=days)).isoformat()
        to_date = date.today().isoformat()
        data = self._get("/stock/upgrade-downgrade",
                         {"symbol": symbol, "from": from_date, "to": to_date})
        return data if isinstance(data, list) else []

    def get_basic_financials(self, symbol: str) -> Optional[dict]:
        """Return basic financial metrics including revenue growth."""
        data = self._get("/stock/metric", {"symbol": symbol, "metric": "all"})
        if not data:
            return None
        return data.get("metric", {})
