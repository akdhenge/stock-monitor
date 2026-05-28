# Stock Monitor

A PyQt5 desktop app for monitoring a personal stock watchlist, running automated stock scans, and generating AI-powered research reports. Includes a Telegram bot interface so you can check your portfolio and trigger scans from your phone.

---

## Features

- **Watchlist** — Track stocks with custom buy/sell price targets; get alerted when a price crosses a threshold
- **Smart Scanner** — Scan S&P 500 / full market universe for undervalued opportunities using a multi-factor scoring model
- **AI Research** — Generate a detailed LLM-written research report for any stock (supports Ollama, Claude API, or OpenRouter)
- **Telegram Bot** — Full remote control: add/remove stocks, trigger scans, run AI research, get alerts — all from Telegram
- **Congressional Trading Signal** — Bonus scoring signal based on tracked politicians' recent buy/sell disclosures
- **Drawdown Screener** *(extra module)* — Finds quality stocks hammered by sentiment-driven, non-fundamental concerns; ranked candidate list with LLM cause classification

---

## Requirements

- Python 3.12
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
py -3.12 main.py
```

On Windows with multiple Python versions, use the launcher (`py -3.12`) to ensure the right interpreter.

---

## App Interface

The app has four main tabs: Watchlist, Smart Scanner, Drawdown Screener, and Logs.

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

### Tab 3 — Drawdown Screener *(extra module)*

> **What it is:** A candidate-finder that screens the S&P 500 for quality stocks that have dropped significantly from a recent high due to a **sentiment-driven, non-fundamental concern** — where the underlying business is intact and analyst conviction has not followed the price down.
>
> **What it is not:** An auto-trader, an alpha guarantee, or a substitute for your own judgment. Final trade decisions stay with you. Backtest before trading real money.

#### The thesis

A specific, repeatable setup:
- Stock is 20–50% below its **recent** 52-week high (set within the last 6 months)
- Business fundamentals are intact — revenue growing, earnings beat
- The drop has a **named, non-fundamental cause** (capex fear, sector rotation, one-time event, macro panic)
- Analyst consensus has **not fallen with the price** — targets imply large upside, ratings overwhelmingly Buy

The signature: a company beats earnings *and the stock still drops* because the market is repricing a sentiment concern, not a fundamentals concern. That divergence is the signal.

#### Five sequential gates (run cheapest-first)

| Gate | What it checks | Data source |
|---|---|---|
| **Gate 2** *(runs first)* | 20–50% below 52w high, high set within 180 days | yfinance batch download |
| **Gate 3** | Revenue growth > 10% YoY, positive operating cash flow, market cap > $10B, earnings beat | yfinance + Finnhub |
| **Gate 4** | Analyst consensus target implies > 25% upside, ≥ 70% Buy ratings, ≥ 10 analysts covering | yfinance + Finnhub |
| **Gate 1** | Options chains exist at 6-month and 12-month expirations | yfinance options |
| **Gate 5** | LLM classifies the cause of the drop as sentiment-driven, not fundamental | DeepSeek API + Alpaca news |

Gate 5 is optional — if no DeepSeek API key is configured, it is skipped and all quantitative survivors are shown without a cause label.

#### Acceptable vs. unacceptable drop causes (Gate 5)

| Acceptable (pass) | Unacceptable (reject) |
|---|---|
| Capex / investment concern | Demand decline / volume miss |
| Margin pressure from cost side | Competitive share loss |
| Sector rotation / multiple compression | Product failure or recall |
| One-time legal / regulatory event | Accounting irregularity |
| Macro / broad market panic | Executive departure under bad circumstances |
| Single guidance cut on non-core metric | Existential regulatory threat |
| Unclear (passes with low confidence) | Secular industry decline |

#### Composite score (0–100)

```
Score = Analyst Upside × 40% + Fundamentals × 25% + Drawdown Attractiveness × 20% + Options Liquidity × 15%
```

Drawdown attractiveness peaks at ~27% below the high (bell curve — both shallow and deep drawdowns score lower).

#### Output

- **Ranked Candidates table** — all stocks that passed all 5 gates, sorted by score
- **Rejected-but-Close list** — stocks that passed 4 of 5 gates (worth monitoring; may qualify next run)
- **Detail pane** — click any row to see full metrics, sub-scores, and the LLM cause summary

#### Setup required

1. Get a free Finnhub API key at **finnhub.io** (no credit card required)
2. Get a DeepSeek API key at **platform.deepseek.com** (very cheap — ~$0.02 per full scan run)
3. Enter both in **Settings → AI → Drawdown Screener**
4. Click **Run Screener** in the Drawdown Screener tab

#### Cost

| Item | Cost |
|---|---|
| Finnhub (earnings + analyst data) | Free tier, no cost |
| DeepSeek-chat (Gate 5 LLM, ~20 stocks) | ~$0.02 per scan |
| yfinance (price + fundamentals) | Free |

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
| **AI** | Provider | `ollama`, `claude`, or `openrouter` — used by AI Research and Smart Scanner ranking |
| **AI** | Ollama URL / model | Local Ollama endpoint and model name |
| **AI** | Claude API key / model | Anthropic API credentials |
| **AI** | OpenRouter key / model | OpenRouter credentials |
| **Congressional** | Tracked politicians | Comma-separated list of politician names whose trades are factored into scan scores |
| **Drawdown Screener** | DeepSeek API key | API key for Gate 5 LLM cause classification (platform.deepseek.com) |
| **Drawdown Screener** | DeepSeek model | Model name (default: `deepseek-chat`) |
| **Drawdown Screener** | Finnhub API key | Free key for earnings surprise + analyst rating data (finnhub.io) |

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
- Smart Scanner scoring formula: `total_score = value×0.4 + growth×0.3 + technical×0.3` (each sub-score 0–100)
- Drawdown Screener is a fully isolated module — removing it requires deleting 4 files and 3 lines from `main_window.py`
- AI Research results are cached 6 hours in `data/ai_research_cache.json`
- Telegram bot uses long-polling (`getUpdates`) — no webhook required
- See `CLAUDE.md` for full architecture reference

---

*Not financial advice. This tool is for personal research and learning purposes only.*
