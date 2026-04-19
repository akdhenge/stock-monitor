# Stock Monitor

## Status
- **State:** ✅ Active
- **Last updated:** 2026-04-17
- **Repo:** https://github.com/akdhenge/stock-monitor (branch: master)
- **Entry point:** `py -3.9 main.py`

## Overview
PyQt5 desktop app for monitoring a stock watchlist, running scheduled scans, and generating AI-powered research reports. All state lives in `data/` (JSON, auto-created on first run).

## Stack
- **Language:** Python 3.9 (use `py -3.9` launcher on Windows)
- **UI:** PyQt5 5.15
- **Data sources:** yfinance, Google News RSS, feedparser, House/Senate Stock Watcher S3
- **AI backends:** Ollama (local, default: `gemma4:26b`) or Claude API (`claude-haiku`) or OpenRouter
- **Alerts:** Telegram bot + Email

## Architecture

### Threading Model
All long-running ops are `QThread` subclasses. Cross-component communication via Qt signals/slots only.

| Thread                  | File                                   | Purpose                                                                 |
| ----------------------- | -------------------------------------- | ----------------------------------------------------------------------- |
| `PricePoller`           | `core/price_poller.py`                 | Polls yfinance prices; emits `prices_updated`                           |
| `StockScanner`          | `core/stock_scanner.py`                | Quick/Deep/Complete scans; emits scan results + alerts                  |
| `AIResearcher`          | `core/ai_researcher.py`                | Fetches enrichment data + calls LLM; emits `research_complete`          |
| `AIFollowUp`            | `core/ai_followup.py`                  | Lightweight follow-up Q&A on cached research; emits `followup_complete` |
| `TelegramCommandPoller` | `notifiers/telegram_command_poller.py` | Long-polls Telegram; emits typed signals per command                    |

### Scan Modes
- **Quick** — batch price pass over S&P 500; manual only
- **Deep** — full fundamental + technical scoring (~500 stocks); scheduled hourly; alerts on new top-10 or score ≥ threshold
- **Complete** — full scoring over ~1,500 stocks (S&P 500/400/600, NASDAQ-100); fixed ET times; alerts on new top-5

Scoring: `total_score = value×0.4 + growth×0.3 + technical×0.3 + congressional_bonus` (each sub-score 0–100; congressional bonus 0–15 pts)

### Persistence (`data/`)
| File | Purpose | TTL |
|---|---|---|
| `settings.json` | All app settings with defaults | — |
| `watchlist.json` | User's stock watchlist | — |
| `scan_results.json` | Persisted scan results | — |
| `ai_research_cache.json` | AI research cache | 6 hr |
| `house_trades_cache.json` | House of Representatives STOCK Act trades | 24 hr |
| `senate_trades_cache.json` | Senate STOCK Act trades | 24 hr |

## Telegram Bot Commands

| Command                          | Description                                                                                 |
| -------------------------------- | ------------------------------------------------------------------------------------------- |
| `/add SYMBOL LOW HIGH [notes]`   | Add stock to watchlist. If already added, **updates** low/high prices instead of rejecting. |
| `/remove SYMBOL`                 | Remove stock from watchlist                                                                 |
| `/list`                          | Show current watchlist                                                                      |
| `/scan`                          | Trigger a quick scan                                                                        |
| `/top`                           | Show top scan results                                                                       |
| `/detail`                        | Full detailed scan table                                                                    |
| `/aiscan SYMBOL`                 | Run AI research on a stock; opens 30-min follow-up session                                  |
| `/stopaiscan`                    | End the active `/aiscan` follow-up session immediately                                      |
| `/mute SYMBOL`                   | Silence all price alerts for that symbol for the rest of today; auto-resets at midnight     |
| `/revise SYMBOL low\|high PRICE` | Update a watchlist low or high target; resets cooldown so new target fires immediately      |
| *(plain text)*                   | Follow-up question after `/aiscan` — answered with AI context for 30 minutes                |

### Telegram Conventions
- HTML parse mode (`<b>`, `<i>` tags)
- 4096-char limit handled by chunking at ~3800 chars
- Command polling only starts when `telegram_command_polling_enabled = true` in settings

## AI Research Flow
1. `/aiscan SYMBOL` → fetches yfinance news + Google News RSS (7 days, deduplicated)
2. Enriches with: price/volume, analyst ratings, insider transactions, options chain (2 expiries), macro/sector news, **short interest** (`shortRatio`, `shortPercentOfFloat`), **earnings surprise history** (last 4 quarters), **congressional trades** (House + Senate, filtered to tracked politicians)
3. Builds structured prompt → calls Ollama / Claude API / OpenRouter
4. Parses structured response: `SHORT_TERM`, `LONG_TERM`, `CATALYSTS`, `SENTIMENT`, `DIRECTION`, `TIMEFRAME`, `CONGRESSIONAL_SIGNAL`, `STOCK_STRATEGY`, `OPTIONS_STRATEGY`, `SUMMARY`
5. Caches result for 6 hours; sends formatted Telegram message
6. Opens 30-minute **follow-up session** — user sends plain-text questions, answered via `AIFollowUp` using cached context

## Congressional Trading Signal

Tracks House and Senate STOCK Act disclosures as an investment signal. Data is free, no API key required.

### Data Sources
| Chamber | URL |
|---|---|
| House | `https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json` |
| Senate | `https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json` |

Both JSONs are downloaded once per scan run and cached 24h locally.

### Scoring
- `score_congressional`: 50 pts per tracked-politician buy (capped 100), −20 per sell (floored 0)
- Applied as bonus on top of base score: `(score_congressional / 100.0) × 15.0`
- 1 buy → **+7.5 pts**; 2+ buys → **+15 pts** max

### Configuration
- `Settings → AI tab → Tracked Politicians` (one name per line, `QPlainTextEdit`)
- Setting key: `congressional_tracked_politicians` (comma-separated internally)
- Empty list = track **all** politicians
- Default list: Nancy Pelosi, Tommy Tuberville, Josh Gottheimer, Michael McCaul, John Hickenlooper, Tom Cotton

### Name Matching
Fuzzy substring match; handles both `"FirstName LastName"` and `"LastName, FirstName"` (Senate format). Checks individual words too — `"Tommy Tuberville"` matches `"Tuberville, Tommy"`.

## Recent Changes

### 2026-04-17 — Lookup Table Separated into Independent Widget
- **Bug fixed:** "Add Selected to Watchlist" silently did nothing when a looked-up ticker was selected — `_on_add_selected` only searched `self._results`, so lookup selections were dropped. Also the empty-selection guard only checked the scan table, showing a false "select rows first" dialog.
- Previous approach embedded lookup rows directly in the scan `QTableWidget` with `setSpan()` header rows — these got reordered by column sorts, corrupting the layout.
- Replaced with `self._lookup_table`: a fully independent `QTableWidget` (same columns, `maxHeight=110`) housed in `self._lookup_section` (QWidget with blue "🔍 Lookup Results" label), shown above the scan table, hidden when empty.
- `_populate_lookup_table()` fills the lookup table and drives `setVisible()` on the section widget.
- All interactive slots updated to handle both tables: `_on_add_selected`, `_on_selection_changed`, `_on_current_item_changed` (uses `current.tableWidget()`), `_show_context_menu` (uses `self.sender()`), `set_research_light`.
- AI Research button and right-click context menu both work on lookup table rows.
- **Files changed:** `gui/smart_scanner_panel.py`

### 2026-04-17 — Ticker Lookup Feature
- Added a "Lookup Ticker" input row to the Smart Scanner panel: `QLineEdit` + Lookup/Clear buttons between the progress bar and results table.
- Typing a ticker and pressing Enter (or Lookup) fetches and scores that symbol in the background via a new `TickerLookupWorker(QThread)`, then displays all attributes (Total, Value, Growth, Tech, P/E, PEG, D/E, RevGrow%, ROE%, RSI) as a blue-highlighted row pinned at the top of the results table.
- Rank column shows `L`; AI Rank and Research columns show `—`/red dot (no scan context).
- Multiple lookups accumulate; re-looking up a symbol replaces its row; Clear button removes all lookup rows.
- "Add Selected to Watchlist" works on lookup rows — selecting a lookup row and clicking the button adds it to the watchlist as normal.
- `TickerLookupWorker` reuses `StockScanner` private methods (`_analyze_symbol`, `_score_value`, `_score_growth`, `_score_technical`) directly — no scoring logic duplicated.
- Lookup results live in `self._lookup_results` (separate from `self._results`) so scan refreshes don't wipe them. Results are in-memory only — cleared on app restart.
- Congressional scoring skipped for lookups; `sector_median_pe=None` (no sector context for single ticker).
- Also added `.claude/launch.json` capturing `py -3.9 main.py` as the single runnable configuration.
- **Files changed:** `core/ticker_lookup.py` *(new)*, `gui/smart_scanner_panel.py`, `gui/main_window.py`

### 2026-04-16 — AI Ranking Status Block, Inline Research Panel, Log Filter, Bug Fixes
- **AI ranking status block**: two-line status widget in Smart Scanner tab (blue pill + grey detail label) shows current stock, progress counter, and per-stock status updates emitted by `AIResearcher.research_status` signal. Fades after 5s on completion.
- **Ollama retry on timeout**: `_call_ollama` now retries once if the 120 s HTTP timeout fires. Status label shows "Ollama timed out — retrying (attempt 2/2)…" so the user has real-time feedback.
- **Empty-response guard**: if Ollama returns `{"response": ""}`, `ai_researcher.py` now skips caching and emits `research_error` instead of writing a useless entry with a valid timestamp (which caused the "green light but blank panel" symptom).
- **Inline research panel bug fixed**: `_on_selection_changed` only enables/disables buttons; row-change → panel update now uses `currentItemChanged` (which fires even when clicking the same row again) instead of `itemSelectionChanged`. Symbol lookup uses `selectedItems()` not `item(selectedIndexes().row(), col)` to avoid row-index mismatch after column sorting.
- **Auto deep-scan on startup suppressed**: `_load_saved_scan_results` now seeds `_last_deep_scan_dt` from the most recent saved result timestamp so the scheduler doesn't trigger an immediate scan on launch.
- **Auto AI ranking freshness check**: `_start_auto_ai_ranking` checks `get_cached_entry()` for each top-10 stock; skips the whole run if all entries are fresher than `ai_rank_refresh_hours` (default 4 h, configurable in Settings → AI tab).
- **Log panel level filter**: `QComboBox` (INFO / WARNING / DEBUG) added to `gui/log_panel.py` top row. Defaults to INFO, suppressing yfinance/urllib3 debug noise.
- **`ai_rank_refresh_hours` setting**: added to `core/settings_store.py` defaults and exposed in `gui/settings_dialog.py` (AI tab → Auto AI Ranking group, spinbox 1–48 hrs).
- **Files changed:** `core/ai_researcher.py`, `core/settings_store.py`, `gui/log_panel.py`, `gui/smart_scanner_panel.py`, `gui/main_window.py`, `gui/settings_dialog.py`

### 2026-04-15 — Log Panel + AI Ranking Diagnostics
- Added `gui/log_panel.py`: `LogPanel` widget with `QtLogHandler` and `_SignalBridge(QObject)` for thread-safe log delivery to the UI via Qt signal queuing
- New "Logs" tab (#3) in `MainWindow`; `setup_log_handler()` attaches to Python root logger so all modules feed into the panel automatically
- Log records are level-colored via `QTextCharFormat`; buffer capped at 2000 lines (trimmed 200 at a time)
- Added structured `logging` calls throughout `core/ai_researcher.py` (cache hits, LLM calls with provider/model, errors, completion with sentiment/direction) and `gui/main_window.py` (AI ranking flow)
- Root-caused blank AI Rank after deep scans — see Issues & Fixes entry 2026-04-15; fix is deferred, visibility is the immediate deliverable

### 2026-04-13 — Congressional Trading Signal + New AI Research Sources
- **Congressional signal**: House + Senate STOCK Act disclosures added as a scanner scoring bonus and AI research section. Scanner pre-loads both JSONs once per scan run. Tracked politicians user-configurable in Settings.
- **New AI research signals**: short interest (`shortRatio`, `shortPercentOfFloat`), earnings surprise history (last 4 quarters beat/miss), congressional trades — all injected into LLM prompt as new sections.
- **`CONGRESSIONAL_SIGNAL` LLM field**: added to response format; shown as colored badge in AI Research dialog and as a line in Telegram `/aiscan` reply.
- **AI model switch**: `qwen3-coder:30b` → `gemma4:26b` as default Ollama model. General-purpose reasoning produces better financial narratives than a code-specialized model.
- **Files changed:** `core/ai_researcher.py`, `core/stock_scanner.py`, `core/scan_result.py`, `core/settings_store.py`, `gui/settings_dialog.py`, `gui/main_window.py`, `gui/ai_research_dialog.py`

### 2026-03-12 — Alert Snooze via Telegram
- **`/mute SYMBOL`** — calls `AlertManager.mute_symbol()`; stores `symbol → today's date` in `_muted` dict; `check_and_alert()` skips muted symbols; stale entries auto-clear when the date changes.
- **`/revise SYMBOL low|high NEW_PRICE`** — updates watchlist entry's `low_target` or `high_target`, resets `last_low_alert`/`last_high_alert` to `None` so new target fires immediately.
- Every Telegram alert now appends a hint with `/revise` and `/mute` commands.
- **Files changed:** `core/alert_manager.py`, `notifiers/telegram_notifier.py`, `notifiers/telegram_command_poller.py`, `gui/main_window.py`

### 2026-03-10 — `/add` Update Behavior + `/aiscan` Follow-up Q&A
- `/add` for an existing symbol now updates low/high targets instead of rejecting. Reply changes from ✅ Added → ✏️ Updated.
- After `/aiscan`, bot invites follow-up questions (30-min window). Plain-text messages routed to `AIFollowUp` thread using cached research as context. New file: `core/ai_followup.py`.

## Issues & Fixes

### 2026-04-17 — setSpan() header rows not sort-immune; lookup Add-to-Watchlist silently broken
**Symptom 1:** After adding section-header rows via `setSpan()` to the scan `QTableWidget`, clicking any column header reordered those rows along with data rows, corrupting the visual layout.
**Root cause:** `setSpan()` merged cells are still regular rows in Qt's sort model — they move with the sort.
**Fix:** Replaced with a fully separate `QTableWidget` for lookup results placed above the scan table.

**Symptom 2:** Selecting a lookup result and clicking "Add Selected to Watchlist" showed "select rows first" or silently added nothing.
**Root cause:** `_on_add_selected` only searched `self._results`; the empty-selection guard only checked `self._table.selectedIndexes()`.
**Fix:** Both checks updated to cover `self._lookup_table` as well.

### 2026-04-16 — Inline research panel always stuck on "No cached research" (actual root cause)
**Symptom:** After running AI research via popup, the inline panel still shows "No cached research" for that stock even though the popup displayed results correctly.

**Root causes (two separate bugs):**
1. `_ResearchPanel._render()` populated widgets on page 2 of the `QStackedWidget` but never called `self._stack.setCurrentIndex(2)`. Data was written to hidden widgets; the panel stayed on page 0 or 1 indefinitely. **Fix:** added `self._stack.setCurrentIndex(2)` at end of `_render()`.
2. `_on_current_item_changed` used `self._table.selectedItems()` to find the symbol. Qt fires `currentItemChanged` *before* updating the selection state, so `selectedItems()` returns stale/empty results — `show_symbol()` was never called and `_current_symbol` was never set, causing `refresh_if_showing()` to skip the update. **Fix:** replaced `selectedItems()` loop with `self._table.item(current.row(), _COL_IDX["Symbol"])` — uses the signal parameter directly, independent of selection-state timing.

**Earlier incomplete fix (also 2026-04-16):** an earlier attempt moved the update to `currentItemChanged` and switched from `selectedIndexes().row()` to `selectedItems()`, but `selectedItems()` has the same staleness problem. Only `current.row()` is reliable inside this handler.

### 2026-04-16 — Empty Ollama response cached with valid timestamp (green light, blank content)
**Symptom:** AI Rank column shows green for a stock, but clicking opens an empty research panel.

**Root cause:** Ollama occasionally returns `{"response": ""}`. `_parse_response("")` produces all-empty fields; `save_entry` writes the dict with a valid `cached_at` timestamp. On next run `get_cached_symbols()` sees it as a fresh hit → green light → `render_research_html` finds no content → blank HTML.

**Fix:** Added guard in `ai_researcher.py` after `_parse_response`: if none of `short_term`, `long_term`, `summary` are non-empty, emit `research_error` and return without calling `save_entry`. Added fallback message in `_ResearchPanel._render` for already-cached empty entries.

### 2026-04-16 — Auto deep scan fires on every app launch
**Symptom:** App launches, loads saved results showing top-10 stocks, then immediately triggers a new deep scan within 60 seconds regardless of how recent the last scan was.

**Root cause:** `_last_deep_scan_dt` initialises to `None`; `_check_scheduled_scans` interprets `None` as "never scanned" and fires immediately.

**Fix:** `_load_saved_scan_results` now seeds `self._last_deep_scan_dt` from the timestamp of the most recent loaded scan result, so the scheduler treats the saved scan as the last real run.

### 2026-04-16 — AI Rank blank after deep scan (✅ Fixed)
**Symptom:** AI Rank column remains `--` after a deep or complete scan completes.

**Root cause (confirmed 2026-04-15):** `_start_auto_ai_ranking` was spawning all 10 `AIResearcher` threads simultaneously. Ollama serialises LLM requests internally — later threads queued for 10–18 min then hit the 120 s HTTP timeout. Errors were silently swallowed.

**Fix (2026-04-16):**
- `_start_auto_ai_ranking` now uses a sequential queue (`_ai_rank_queue_idx` counter); only one `AIResearcher` runs at a time.
- `_call_ollama` retries once on timeout, with status updates shown in the new AI ranking status block.
- `research_status` signal on `AIResearcher` surfaces per-stock progress and retry/error details in the Smart Scanner status block.
- Freshness check at queue start: if all top-10 have cache entries < `ai_rank_refresh_hours` old, the whole run is skipped.

### 2026-04-13 — Senate JSON senator field inconsistency
Senate `senator` field is sometimes `"Tuberville, Tommy"` (string) and sometimes `{"first_name": ..., "last_name": ...}` (dict). Parser handles both cases explicitly.

### 2026-04-13 — `ticker == "--"` in congressional JSONs
Both House and Senate JSONs use `--` as the ticker for non-stock assets (real estate, funds, etc.). Filtered out before any symbol matching.

### 2026-04-13 — Old `congressional_trades_cache.json` orphaned
Previous implementation wrote a single combined cache. Refactored into `house_trades_cache.json` + `senate_trades_cache.json`. Old file is unused but not deleted automatically.

### Earlier
- yfinance news items sometimes have no `providerPublishTime` — handled with fallback to `datetime.now()`
- Qwen3 model emits `<think>...</think>` blocks — stripped via regex before response parsing

## File Structure
```
stock-monitor/
├── main.py                   # Entry point
├── core/
│   ├── ai_researcher.py      # AI research QThread (full data fetch + LLM)
│   ├── ai_followup.py        # AI follow-up QThread (lightweight, uses cache)
│   ├── stock_scanner.py      # Scan modes + scoring (incl. congressional bonus)
│   ├── ticker_lookup.py      # TickerLookupWorker — on-demand single-ticker scoring
│   ├── price_poller.py       # Price polling
│   ├── models.py             # StockEntry, AlertRecord
│   ├── scan_result.py        # ScanResult dataclass (incl. score_congressional)
│   ├── settings_store.py     # Settings with defaults
│   ├── watchlist_store.py    # Watchlist persistence
│   ├── scan_results_store.py # Scan results persistence
│   └── ai_research_store.py  # AI cache (6 hr TTL)
├── gui/
│   ├── main_window.py        # Central coordinator, all signal wiring
│   ├── log_panel.py          # Log panel widget + Qt logging handler
│   ├── smart_scanner_panel.py
│   ├── ai_research_dialog.py # AI research result display (incl. congressional badge)
│   ├── settings_dialog.py    # Settings UI (incl. tracked politicians field)
│   └── ...
├── notifiers/
│   ├── telegram_notifier.py
│   ├── telegram_command_poller.py
│   └── email_notifier.py
└── data/                     # Auto-created, gitignored
    ├── house_trades_cache.json
    └── senate_trades_cache.json
```

## Links & Resources
- House Stock Watcher: https://housestockwatcher.com
- Senate Stock Watcher: https://senatestockwatcher.com

---
[[Automation MOC]] | [[Scripts & API Integrations]] | [[Home]]
