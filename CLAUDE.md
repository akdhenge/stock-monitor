# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

```bash
py -3.12 main.py
```

Use `py -3.12` (not `python`) — this machine uses the Windows Python Launcher and has multiple Python versions installed. All dependencies (PyQt5, yfinance, etc.) are installed under Python 3.12.

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
| `DrawdownScanner` *(extra module)* | `core/drawdown_scanner.py` | Screens S&P 500 through 5 sequential gates; emits `scan_complete` with ranked `DrawdownResult` list |

All threads use the pattern: loop with `self._running` flag, sleep in 1-second chunks so `stop()` is responsive.

### Scan Modes (`core/stock_scanner.py`)

- **Quick** — batch price pass over S&P 500; manual only; emits `quick_scan_complete`
- **Deep** — full fundamental + technical scoring, S&P 500 (~500 stocks); scheduled hourly; alerts on new top-10 entry or score >= threshold
- **Complete** — full scoring over up to 1,500 stocks (4 indices); scheduled at fixed ET times; alerts on new top-5 entry

Scoring formula: `total_score = value*0.4 + growth*0.3 + technical*0.3` (each sub-score 0–100).

Universe is fetched from Wikipedia index pages (S&P 500/400/600, NASDAQ-100) with a hardcoded fallback in `_FALLBACK_SYMBOLS`.

### Data Flow: Telegram Commands

`TelegramCommandPoller` emits signals (e.g. `cmd_scan`, `cmd_aiscan`) → `MainWindow` slots handle business logic → `TelegramNotifier.send_message()` replies. The poller is only started when `telegram_command_polling_enabled` is true in settings.

**Available bot commands:**

| Command | Description |
|---|---|
| `/add SYMBOL LOW HIGH [notes]` | Add stock to watchlist |
| `/remove SYMBOL` | Remove stock from watchlist |
| `/list` | List watchlist |
| `/scan` | Trigger a quick scan |
| `/top` | Show top scan results |
| `/detail` | Show detailed scan table |
| `/aiscan SYMBOL` | Run AI research on a symbol; opens a 30-minute follow-up Q&A window |
| `/stopaiscan` | End the active `/aiscan` follow-up session immediately (frees memory) |
| `/mute SYMBOL` | Silence price alerts for that symbol for the rest of today (auto-resets at midnight) |
| `/revise SYMBOL low\|high NEW_PRICE` | Update a watchlist entry's low or high target price; resets cooldown so new target takes effect immediately |
| `/spreads` | List pending debit-spread proposals (entry + close) and open debit-spread positions |
| `/approvespread SYMBOL` | Approve a pending debit-spread entry proposal — the only path that submits the order |
| `/closespread SYMBOL` | Close an open debit-spread position — the only path that closes it |
| (plain text) | Follow-up question for the active `/aiscan` session |

**`/aiscan` follow-up session lifecycle:** After `/aiscan` completes, `MainWindow._on_aiscan_complete` calls `TelegramCommandPoller.register_followup_session()` which stores `{symbol, expires}` per `chat_id`. Plain-text messages are routed to `cmd_aifollow` → `AIFollowUp` QThread. `/stopaiscan` emits `cmd_stopaiscan` → `_on_cmd_stopaiscan` which clears both `_aiscan_context[chat_id]` and the poller session, freeing memory immediately.

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
- `DrawdownResult` (`core/drawdown_result.py`) *(extra module)* — dataclass for drawdown screener candidates; includes `failed_gate` field to distinguish close-misses from passed candidates
- Settings dict keys are defined in `_DEFAULTS` in `core/settings_store.py`

### Drawdown Screener + Debit-Spread Sleeve

**No longer isolated** — as of the debit-spread integration, this module feeds half the trading account's capital (the `options_debit_spread` sleeve) and is wired into `TraderAgent`, `TelegramCommandPoller`, and `MainWindow`'s scheduler. It is still the only source of entries for that sleeve, and it never auto-executes — every order-touching action requires an explicit Telegram command.

**Files:**
- `core/drawdown_result.py`, `core/drawdown_scanner.py`, `core/finnhub_client.py`, `gui/drawdown_screener_panel.py` — the screener itself (GUI-manual trigger unchanged)
- `core/daily_drawdown_runner.py` — QThread firing the headless screen once/day at `drawdown_scan_time_et` (mirrors `core/ta_batch_runner.py`'s daily-fire pattern)
- `core/drawdown_spread_builder.py` — builds a concrete bull call spread (strikes/expiration/net debit/max profit-loss) from a `DrawdownResult`, using yfinance chain data (same pattern as `core/options_strategy.py` — there is no Alpaca options-chain fetch path in this codebase; `core/options_executor.py` only submits/manages orders)
- `core/debit_spread_proposals_store.py` — persists pending entry/close proposals awaiting a Telegram decision (`data/debit_spread_proposals.json`)
- `core/debit_spread_trader.py` — orchestration: `propose_entries()` / `check_exits()` (propose-only, called once/day from `TraderAgent._process_propose_spreads`), and `approve_spread()` / `close_spread()` — the **only** functions in the app allowed to call `OptionsExecutor.submit_spread()` / `close_option_position()`, and both are only ever invoked from an explicit `/approvespread` or `/closespread` Telegram command
- Open debit-spread positions are tracked in the existing `core/options_portfolio.py` store (`strategy_type="bull_call_spread"`), parallel to the legacy multi-strategy options positions it already holds

**What the screener does:** Screens S&P 500 for stocks down 20–50% from a recent high due to sentiment concerns (not fundamental damage). Five sequential gates filter from ~500 symbols to ~5–20 ranked candidates.

**Gate order (cheapest API first):**

| Gate | Filter | Source |
|---|---|---|
| Gate 2 | 20–50% below 52w high, within 180 days | yfinance batch download |
| Gate 3 | Rev growth > 10%, op. cash flow > 0, market cap > $10B, earnings beat | yfinance + Finnhub |
| Gate 4 | Analyst upside > 25%, Buy% ≥ 70%, ≥ 10 analysts | yfinance + Finnhub |
| Gate 1 | Real options liquidity: OI > 500 and bid/ask spread < 5% of mid at near-ATM strikes on 6m/12m expirations | yfinance `.option_chain()` |
| Gate 5 | LLM classifies drop cause as non-fundamental | DeepSeek API + Alpaca news |

**Gate 5 is optional** — if `deepseek_api_key` is not set, it is skipped and all Gate 1–4 survivors are returned without a cause label.

**Composite score:** `analyst_upside×0.40 + fundamentals×0.25 + drawdown_bell×0.20 + options×0.15`
Drawdown attractiveness uses a Gaussian bell peaking at ~27% below the high.

**Headless entrypoint:** `core.drawdown_scanner.run_screen_headless(settings)` instantiates a `DrawdownScanner` and calls its private `_do_scan()` pipeline method directly (no `.start()`/QThread event loop) — used by `DailyDrawdownRunner` so the GUI-manual `DrawdownScanner` QThread path is untouched.

**Debit-spread flow:** `DailyDrawdownRunner` (daily, `drawdown_scan_time_et`) → `MainWindow._on_daily_drawdown_complete` persists/displays results and calls `TraderAgent.queue_propose_spreads()` → `TraderAgent._process_propose_spreads` runs `debit_spread_trader.propose_entries()` (sizes against the `options_debit_spread` sleeve via `capital_ledger.sleeve_budget_remaining`, capped by `debit_spread_max_loss_pct` of sleeve NAV per trade, `drawdown_max_candidates_per_cycle` candidates/day) and `check_exits()` (profit-take, target-proximity, close-DTE, max-hold-days, and qualitative "candidate broke down" flags) — both push Telegram messages only. `/approvespread` and `/closespread` route through `TraderAgent.queue_approve_spread()` / `queue_close_spread()` so the actual Alpaca calls stay on `TraderAgent`'s single-owner thread.

**Settings/config keys added:** `deepseek_api_key`, `deepseek_model`, `finnhub_api_key`, `drawdown_min_market_cap_b` (settings.json); `stock_sleeve_pct`, `options_sleeve_pct`, `debit_spread_max_loss_pct`, `debit_spread_max_hold_days`, `debit_spread_profit_take_pct`, `debit_spread_target_proximity_pct`, `drawdown_scan_time_et`, `drawdown_max_candidates_per_cycle` (trader_config.json)

**Cause taxonomy** — acceptable (pass): `capex_concern`, `margin_pressure`, `sector_rotation`, `one_time_legal`, `macro_panic`, `guidance_cut`, `unclear`. Unacceptable (reject): `demand_decline`, `share_loss`, `product_failure`, `accounting`, `exec_departure`, `existential_regulatory`, `secular_decline`.

### Telegram Message Conventions

- HTML parse mode (`<b>`, `<i>` tags)
- 4096-char limit handled by chunking at ~3800 chars
- UTF-8 encoding throughout (emojis used in messages)

### MainWindow (`gui/main_window.py`)

Central coordinator: holds references to all QThread instances (preventing GC), manages scheduler via a 60-second `QTimer`, and connects all signals. Scanner state (`_scanner_top5`, `_scanner_top10`, `_scanner_prev_scores`) is maintained here for deduplicating alerts across scans.
