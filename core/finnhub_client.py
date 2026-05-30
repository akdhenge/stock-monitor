"""
Thin Finnhub API client for earnings surprise and analyst recommendation data.
Free tier: 60 calls/minute. No caching here — caller is responsible.
"""
import json
import logging
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import List, Optional

_log = logging.getLogger(__name__)

_BASE = "https://finnhub.io/api/v1"


class FinnhubClient:
    def __init__(self, api_key: str):
        self._key = api_key

    def _get(self, path: str, params: dict) -> Optional[dict]:
        params["token"] = self._key
        url = f"{_BASE}{path}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            _log.warning("Finnhub %s failed: %s", path, exc)
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
