"""
AIFollowUp — lightweight QThread for follow-up questions after /aiscan.

Takes the cached research result dict and a plain-text question from the user,
builds a context-aware prompt, calls the configured LLM backend, and emits the
plain-text reply so MainWindow can forward it to Telegram.

Signals:
  followup_complete(str) — LLM's answer
  followup_error(str)    — human-readable error
"""
import json
import re
import urllib.request
from typing import Any, Dict

from PyQt5.QtCore import QThread, pyqtSignal


class AIFollowUp(QThread):
    followup_complete = pyqtSignal(str)
    followup_error = pyqtSignal(str)

    def __init__(
        self,
        symbol: str,
        research: Dict[str, Any],
        question: str,
        settings: Dict[str, Any],
        parent=None,
    ):
        super().__init__(parent)
        self._symbol = symbol
        self._research = research
        self._question = question
        self._settings = settings

    def run(self) -> None:
        prompt = self._build_prompt()
        provider = self._settings.get("ai_provider", "ollama")
        try:
            if provider == "claude":
                response = self._call_claude(prompt)
            else:
                response = self._call_ollama(prompt)
        except Exception as exc:
            self.followup_error.emit(str(exc))
            return

        # Strip Qwen3 think blocks
        response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
        self.followup_complete.emit(response if response else "(No response from AI)")

    def _build_prompt(self) -> str:
        r = self._research
        context = "\n".join([
            f"Symbol: {self._symbol}",
            f"Sentiment: {r.get('sentiment', 'N/A')}",
            f"Direction: {r.get('direction', 'N/A')}",
            f"Timeframe: {r.get('timeframe', 'N/A')}",
            f"Short-term outlook: {r.get('short_term', 'N/A')}",
            f"Long-term outlook: {r.get('long_term', 'N/A')}",
            f"Catalysts: {r.get('catalysts', 'N/A')}",
            f"Stock Strategy: {r.get('stock_strategy', 'N/A')}",
            f"Options Strategy: {r.get('options_strategy', 'N/A')}",
            f"Summary: {r.get('summary', 'N/A')}",
        ])
        return (
            f"/no_think\n\n"
            f"You are a stock research analyst. Below is your recent analysis of {self._symbol}.\n\n"
            f"=== PREVIOUS ANALYSIS ===\n{context}\n\n"
            f"The user has a follow-up question:\n{self._question}\n\n"
            f"Answer concisely and specifically about {self._symbol}. "
            f"If the question is outside the scope of stock/options analysis, politely redirect."
        )

    def _call_ollama(self, prompt: str) -> str:
        url = self._settings.get("ai_ollama_url", "http://localhost:11434/api/generate")
        model = self._settings.get("ai_ollama_model", "mistral")
        payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("response", "")

    def _call_claude(self, prompt: str) -> str:
        api_key = self._settings.get("ai_claude_api_key", "")
        model = self._settings.get("ai_claude_model", "claude-haiku-20240307")
        if not api_key:
            raise RuntimeError("Claude API key not set. Go to Settings → AI.")
        payload = json.dumps({
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            content = body.get("content", [])
            return content[0].get("text", "") if content else ""
