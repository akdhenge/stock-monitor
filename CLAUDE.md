# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
py main.py
```

Use `py` (not `python`) — this machine uses the Windows Python Launcher.

There are no automated tests or lint commands set up for this project.

## Architecture Overview

This is a PyQt5 desktop app for monitoring a stock watchlist, running scheduled scans, and generating AI-powered research reports. All persistent state lives in `data/` (JSON files, auto-created on first run).

### Threading Model

Every long-running operation is a `QThread` subclass. All cross-component communication uses Qt signals/slots — never direct method calls across threads.

| Thread class | File | Purpose |
|---|---|---|
| `PricePoller` | `core/price_poller.py` | Polls yfinance prices on a configurable interval; emits `prices_updated` |
| `StockScanner` | `core/stock_scanner.py` | Runs quick/deep/complete scans; emits scan results and alert signals |
| `AIResearcher` | `core/ai_researcher.py` | Fetches enrichment data and calls LLM; emits `research_complete` / `research_error` |
| `TelegramCommandPoller` | `notifiers/telegram_command_poller.py` | Long-polls Telegram getUpdates; emits typed signals per command |

All threads use the pattern: loop with `self._running` flag, sleep in 1-second chunks so `stop()` is responsive.

### Scan Modes (`core/stock_scanner.py`)

- **Quick** — batch price pass over S&P 500; manual only; emits `quick_scan_complete`
- **Deep** — full fundamental + technical scoring, S&P 500 (~500 stocks); scheduled hourly; alerts on new top-10 entry or score >= threshold
- **Complete** — full scoring over up to 1,500 stocks (4 indices); scheduled at fixed ET times; alerts on new top-5 entry

Scoring formula: `total_score = value*0.4 + growth*0.3 + technical*0.3` (each sub-score 0–100).

Universe is fetched from Wikipedia index pages (S&P 500/400/600, NASDAQ-100) with a hardcoded fallback in `_FALLBACK_SYMBOLS`.

### Data Flow: Telegram Commands

`TelegramCommandPoller` emits signals (e.g. `cmd_scan`, `cmd_aiscan`) → `MainWindow` slots handle business logic → `TelegramNotifier.send_message()` replies. The poller is only started when `telegram_command_polling_enabled` is true in settings.

### AI Research (`core/ai_researcher.py`)

Supports two backends controlled by `ai_provider` setting:
- `"ollama"` — local Ollama REST API (default model: `qwen3-coder:30b`)
- `"claude"` — Anthropic Claude API (default model: `claude-haiku-20240307`)

Results are cached for 6 hours in `data/ai_research_cache.json`. The `AIResearchDialog` (gui) displays the structured output.

### Persistence (all in `data/`)

| File | Store module | Contents |
|---|---|---|
| `settings.json` | `core/settings_store.py` | All app settings with defaults |
| `watchlist.json` | `core/watchlist_store.py` | User's stock watchlist |
| `scan_results.json` | `core/scan_results_store.py` | Persisted scan results |
| `ai_research_cache.json` | `core/ai_research_store.py` | AI research cache (6 hr TTL) |

### Key Data Types

- `ScanResult` (`core/scan_result.py`) — dataclass with all scored fields; `scan_mode` is `"quick"`, `"deep"`, or `"complete"`
- `StockEntry` / `AlertRecord` (`core/models.py`) — watchlist entry and alert history
- Settings dict keys are defined in `_DEFAULTS` in `core/settings_store.py`

### Telegram Message Conventions

- HTML parse mode (`<b>`, `<i>` tags)
- 4096-char limit handled by chunking at ~3800 chars
- UTF-8 encoding throughout (emojis used in messages)

### MainWindow (`gui/main_window.py`)

Central coordinator: holds references to all QThread instances (preventing GC), manages scheduler via a 60-second `QTimer`, and connects all signals. Scanner state (`_scanner_top5`, `_scanner_top10`, `_scanner_prev_scores`) is maintained here for deduplicating alerts across scans.
