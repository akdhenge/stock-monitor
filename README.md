# Stock Monitor

A PyQt5 desktop app for monitoring a personal stock watchlist, running automated stock scans, and generating AI-powered research reports. Includes a Telegram bot interface so you can check your portfolio and trigger scans from your phone.

---

## Features

- **Watchlist** — Track stocks with custom buy/sell price targets; get alerted when a price crosses a threshold
- **Smart Scanner** — Scan S&P 500 / full market universe for undervalued opportunities using a multi-factor scoring model
- **AI Research** — Generate a detailed LLM-written research report for any stock (supports Ollama, Claude API, or OpenRouter)
- **Telegram Bot** — Full remote control: add/remove stocks, trigger scans, run AI research, get alerts — all from Telegram
- **Congressional Trading Signal** — Bonus scoring signal based on tracked politicians' recent buy/sell disclosures

---

## Requirements

- Python 3.9 (dependencies are pinned to 3.9; does **not** work on 3.12+)
- Windows (tested on Windows 11)

Install dependencies:

```bash
pip install PyQt5 yfinance feedparser requests pandas
```

For AI research with a local model, install [Ollama](https://ollama.ai) and pull a model:

```bash
ollama pull gemma4:26b
```

---

## Running the App

```bash
py -3.9 main.py
```

On Windows with multiple Python versions, use the launcher (`py -3.9`) to ensure the right interpreter.

---

## App Interface

The app has two main tabs accessible from the top of the window.

### Toolbar Buttons

| Button | What it does |
|---|---|
| **Add** | Add a new stock to your watchlist |
| **Edit** | Edit the selected watchlist entry (targets, notes) |
| **Remove** | Remove the selected stock from the watchlist |
| **Refresh Now** | Immediately poll current prices for all watchlist stocks |
| **Quick Scan** | Run a quick price-pass scan over S&P 500 |

---

### Tab 1 — Watchlist

Your personal list of stocks to monitor. The app polls prices on a configurable interval (default: every 60 seconds) and alerts you when a price crosses your target.

#### Watchlist Table Columns

| Column | What it means |
|---|---|
| **Symbol** | Stock ticker symbol (e.g. AAPL, TSLA) |
| **Price** | Current market price, refreshed automatically |
| **Low Target** | Your buy-zone floor — you'll be alerted when the price drops to or below this |
| **High Target** | Your sell-zone ceiling — you'll be alerted when the price rises to or above this |
| **Status** | `OK` = price is within your range · `BELOW LOW` = buy signal (green) · `ABOVE HIGH` = sell signal (red) |
| **Notes** | Your personal notes for this stock |

#### Alert History Panel

Appears below the watchlist table. Shows a timestamped log of every price alert fired in the current session. Entries are color-coded: **red** for ABOVE HIGH, **green** for BELOW LOW.

---

### Tab 2 — Smart Scanner

Scans the market for stocks ranked by a composite scoring formula and surfaces the best opportunities.

#### Scan Modes

| Mode | Universe | Trigger | Purpose |
|---|---|---|---|
| **Quick Scan** | S&P 500 (~500 stocks) | Manual only | Fast price-pass; identifies candidates for deeper analysis |
| **Deep Scan** | S&P 500 (~500 stocks) | Runs hourly (if enabled) | Full fundamental + technical scoring; alerts on new top-10 entries |
| **Complete Scan** | Up to 1,500 stocks (4 indices) | Runs at fixed times (if enabled) | Broadest coverage; alerts on new top-5 entries |

Scheduled scans must be enabled in **Settings**.

#### Scoring Formula

```
Total Score = Value × 40% + Growth × 30% + Technical × 30%
```

All sub-scores are on a 0–100 scale. Higher is better.

#### Scanner Table Columns

| Column | What it means |
|---|---|
| **#** | Rank by total score (1 = highest score) |
| **Symbol** | Stock ticker symbol |
| **Total** | Composite score (0–100). Weighted average of Value, Growth, and Technical scores |
| **Value** | Value score (0–100). Derived from P/E ratio, PEG ratio, and Debt/Equity — measures how cheap the stock is relative to fundamentals |
| **Growth** | Growth score (0–100). Derived from revenue growth rate and return on equity — measures business momentum |
| **Tech** | Technical score (0–100). Derived from RSI, MACD signal, proximity to 200-day moving average, and volume spikes |
| **P/E** | Price-to-Earnings ratio. Lower is cheaper relative to earnings. Negative means the company is currently unprofitable |
| **PEG** | Price/Earnings-to-Growth ratio. A PEG below 1.0 generally indicates the stock may be undervalued relative to its growth rate |
| **D/E** | Debt-to-Equity ratio. Lower means less leveraged. Above 2.0 can indicate elevated financial risk |
| **RevGrow%** | Year-over-year revenue growth. Higher means the company is growing faster |
| **ROE%** | Return on Equity. How efficiently the company turns shareholder equity into profit. Higher is better |
| **RSI** | Relative Strength Index (0–100). Below 30 = oversold / potential buy. Above 70 = overbought / potential sell |
| **AI Rank** | AI-generated rank (1 = best). After a deep/complete scan, the top 10 stocks are automatically sent to the LLM for qualitative ranking. Lower number = more compelling opportunity according to the AI |

#### Row Color Coding

| Color | Score Range | Interpretation |
|---|---|---|
| Green | ≥ 65 | Strong opportunity |
| Yellow | 50–64 | Above average |
| Orange | 35–49 | Weak |
| Dark Orange | 20–34 | Poor |
| Red | < 20 | Very weak |

#### Scanner Action Buttons

| Button | What it does |
|---|---|
| **Quick / Deep / Complete Scan** | Start that scan mode |
| **Cancel** | Stop the running scan |
| **Add Selected to Watchlist** | Add selected scanner result(s) to your watchlist |
| **AI Research** | Run a full AI research report on the selected stock (select exactly 1 row) |
| **Export CSV** | Save the current scan results as a CSV file |

---

## AI Research

Click **AI Research** on any stock (in the Scanner tab) to generate a structured report covering:

- Business overview and recent news
- Analyst ratings and price targets
- Key financial metrics
- Recent insider and congressional trading activity
- LLM-written investment thesis (bull case / bear case)

Reports are cached for 6 hours. Results open in a dedicated dialog window.

**Supported AI backends** (configured in Settings):

| Backend | How to use |
|---|---|
| **Ollama** (default) | Install Ollama locally, pull a model (e.g. `gemma4:26b`), set the URL in Settings |
| **Claude API** | Enter your Anthropic API key in Settings |
| **OpenRouter** | Enter your OpenRouter API key in Settings |

---

## Telegram Bot

Control the app remotely via a Telegram bot. To enable:

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token
2. Get your chat ID (send a message to [@userinfobot](https://t.me/userinfobot))
3. Enter both in **Settings → Telegram**
4. Enable "Command Polling"

### Available Commands

| Command | Description |
|---|---|
| `/add SYMBOL LOW HIGH [notes]` | Add a stock to your watchlist with buy/sell targets |
| `/remove SYMBOL` | Remove a stock from your watchlist |
| `/list` | Show your full watchlist with current prices |
| `/scan` | Trigger a quick scan |
| `/top` | Show the current top scan results (summary) |
| `/detail` | Show a detailed scan results table |
| `/aiscan SYMBOL` | Run a full AI research report on a symbol; opens a 30-minute follow-up Q&A session |
| `/stopaiscan` | End the active `/aiscan` follow-up session immediately |
| `/mute SYMBOL` | Silence price alerts for that symbol for the rest of today |
| `/revise SYMBOL low\|high NEW_PRICE` | Update a watchlist entry's buy or sell target |
| *(plain text)* | Ask a follow-up question about the last `/aiscan` result |

---

## Settings

Open via **File → Settings** in the menu bar.

| Section | Setting | Description |
|---|---|---|
| **Polling** | Poll interval | How often (in seconds) to refresh watchlist prices |
| **Polling** | Cooldown | Minimum minutes between repeat alerts for the same stock |
| **Telegram** | Token / Chat ID | Credentials for your Telegram bot |
| **Telegram** | Enable command polling | Whether the bot listens for commands |
| **Scanner** | Universe size | How many stocks to scan (up to 1,500) |
| **Scanner** | Deep scan enabled / interval | Turn on hourly deep scans |
| **Scanner** | Complete scan enabled / times | Turn on scheduled complete scans; set run times in ET (e.g. `09:00,13:00,16:15`) |
| **Scanner** | Alert threshold | Minimum score to trigger a scan alert notification |
| **AI** | Provider | `ollama`, `claude`, or `openrouter` |
| **AI** | Ollama URL / model | Local Ollama endpoint and model name |
| **AI** | Claude API key / model | Anthropic API credentials |
| **AI** | OpenRouter key / model | OpenRouter credentials |
| **Congressional** | Tracked politicians | Comma-separated list of politician names whose trades are factored into scan scores |

---

## Data Files

All persistent data is stored in the `data/` directory (auto-created on first run):

| File | Contents |
|---|---|
| `settings.json` | All app settings |
| `watchlist.json` | Your stock watchlist |
| `scan_results.json` | Most recent scan results (restored on restart) |
| `ai_research_cache.json` | AI research cache (6-hour TTL) |
| `house_trades_cache.json` | Congressional House trading data cache (24-hour TTL) |
| `senate_trades_cache.json` | Congressional Senate trading data cache (24-hour TTL) |

---

## Congressional Trading Signal

When politicians listed under **Settings → Congressional → Tracked politicians** have recently traded a stock, that stock receives a bonus score of up to +15 points in scanner results. The signal uses free public disclosure data (refreshed every 24 hours). This is purely informational — not financial advice.

---

## Architecture Notes (for developers)

- All background work runs in `QThread` subclasses; UI updates go through Qt signals/slots
- Scan scoring formula: `total_score = value×0.4 + growth×0.3 + technical×0.3` (each sub-score 0–100)
- AI Research results are cached 6 hours in `data/ai_research_cache.json`
- Telegram bot uses long-polling (`getUpdates`) — no webhook required
- See `CLAUDE.md` for full architecture reference

---

*Not financial advice. This tool is for personal research and learning purposes only.*
