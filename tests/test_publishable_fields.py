"""
Ensure that serialized publishable payloads never contain secret values.
Run: py -3.9 -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import unittest

# Allow importing from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


_DUMMY_SECRETS = {
    "telegram_token":       "TELEGRAM_SECRET_TOKEN_12345",
    "ai_claude_api_key":    "CLAUDE_SECRET_KEY_ABCDE",
    "ai_openrouter_api_key": "OPENROUTER_SECRET_KEY_XYZ",
    "email_password":       "EMAIL_SECRET_PASSWORD_789",
    "r2_secret_access_key": "R2_SECRET_ACCESS_KEY_FGHIJ",
}


class TestPublishableFields(unittest.TestCase):

    def _get_published_json_text(self, tmp_dir: str) -> str:
        """Return all text from every JSON file under tmp_dir, concatenated."""
        parts = []
        for root, _dirs, files in os.walk(tmp_dir):
            for fname in files:
                if fname.endswith(".json"):
                    with open(os.path.join(root, fname), encoding="utf-8") as f:
                        parts.append(f.read())
        return "\n".join(parts)

    def test_no_secret_leaks_in_published_output(self):
        """Publish to a temp directory and verify no secret value appears in any JSON."""
        import queue as q
        from unittest.mock import patch
        from core.models import AlertRecord, StockEntry
        from core.web_publisher import WebPublisher
        from datetime import datetime

        dummy_settings = {
            "web_publishing_enabled": True,
            **_DUMMY_SECRETS,
        }

        dummy_watchlist = [
            StockEntry(symbol="AAPL", low_target=170.0, high_target=210.0, notes="test")
        ]
        dummy_alerts = [
            AlertRecord(
                timestamp=datetime(2026, 4, 17, 18, 30),
                symbol="AAPL",
                direction="ABOVE HIGH",
                price=212.0,
                target=210.0,
                notified=True,
            )
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            publish_dir = os.path.join(tmp_dir, "web_publish")

            publisher = WebPublisher(
                get_settings=lambda: dummy_settings,
                get_alerts=lambda: dummy_alerts,
            )

            with patch("core.web_publisher._PUBLISH_DIR", publish_dir), \
                 patch("core.web_publisher.load_watchlist", return_value=dummy_watchlist), \
                 patch("core.web_publisher.load_scan_results", return_value=[]), \
                 patch("core.web_publisher.get_cached_symbols", return_value=set()), \
                 patch("core.web_publisher.get_cached_entry", return_value=None):
                publisher._do_publish("manual")

            all_json = self._get_published_json_text(publish_dir)

            for key, secret_value in _DUMMY_SECRETS.items():
                self.assertNotIn(
                    secret_value,
                    all_json,
                    msg=f"Secret value for '{key}' found in published JSON output!",
                )

    def test_get_safe_settings_redacts_all_secrets(self):
        """get_safe_settings() must redact all known secret keys."""
        from core.settings_store import get_safe_settings

        settings = dict(_DUMMY_SECRETS)
        settings["poll_interval_seconds"] = 60

        safe = get_safe_settings(settings)

        for key, secret_value in _DUMMY_SECRETS.items():
            self.assertEqual(
                safe[key], "***",
                msg=f"'{key}' was not redacted by get_safe_settings()",
            )
        self.assertEqual(safe["poll_interval_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
